import re
import time
import os
import sys
import subprocess
import requests
from dotenv import load_dotenv
import Logcat
# 显式加载脚本同目录下的 .env，避免因工作目录不同导致环境变量缺失
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

Log = Logcat.Logcat(outputFile=None)

# ======= 配置（从环境变量读取，见 .env）=======
CPOLAR_PATH = os.environ.get("CPOLAR_PATH")
CPOLAR_FILENAME = os.path.basename(CPOLAR_PATH)
PORT = int(os.environ.get("PORT", "80"))
# cpolar 把日志落盘，Python 读文件抓取地址（已验证有效）
CPOLAR_LOG_PATH = os.environ.get("CPOLAR_LOG_PATH")
CPOLAR_ARGS = ["http", str(PORT), "-log=" + CPOLAR_LOG_PATH]
MAX_RETRY_COUNT = int(os.environ.get("MAX_RETRY_COUNT", "3"))
CF_RETRY_DELAY = int(os.environ.get("CF_RETRY_DELAY", "20"))
CPOLAR_RETRY_DELAY = int(os.environ.get("CPOLAR_RETRY_DELAY", "40"))

CLOUDFLARE_WORKERS_API = os.environ.get(
    "CLOUDFLARE_WORKERS_API", "https://remote.mclhz.de5.net/write"
)
WORKER_PASSWORD = os.environ.get("WORKER_PASSWORD", "")

# 匹配 cpolar 公网地址：无论 Forwarding 行还是 JSON 日志，地址都含 cpolar 域名
# 形如：Forwarding    https://abc.cpolar.cn -> ...  或  "Url":"https://abc.cpolar.top"
# 收尾只用字母数字/点/横线，遇到 " \ 空格等边界字符自动停止，避免多吞一个反斜杠
forwardPattern = re.compile(r"https?://[a-zA-Z0-9.\-]*cpolar\.[a-zA-Z]+[a-zA-Z0-9.\-]*")


def extractAddress(line):
    """从一行 cpolar 日志中提取转发地址，优先返回 https。"""
    httpsAddress = None
    httpAddress = None
    for match in forwardPattern.findall(line):
        if match.startswith("https://"):
            httpsAddress = match
        elif match.startswith("http://"):
            httpAddress = match
    return httpsAddress or httpAddress


def checkAddressAvailable(address):
    """检查隧道地址是否还活着。
    返回 False（视为隧道失效、累计并触发重启）的情况：
      1. 明确 404（cpolar 窗口/隧道已挂）
      2. 网络异常（超时、连接失败、DNS 失败等）
    其余（200、5xx 等）一律返回 True（不当作失败）。"""
    try:
        response = requests.get(address, timeout=10)
        # Log.d("cpolar", f"检查地址: {address}，状态码: {response.status_code}，响应内容: {response.text}")
        if response.status_code == 404:
            Log.d("cpolar", f"地址 {address} 已失效")
            return False
        return True
    except Exception as e:
        Log.e("cpolar", f"检查地址 {address} 失败: {e}")
        return False


def pushAddressToCloudflareWorkers(address):
    """将地址推送到 Cloudflare Workers（带 password 查询参数鉴权）。"""
    payload = {
        "port": PORT,
        "url": address,
    }
    params = {"password": WORKER_PASSWORD}
    response = requests.post(CLOUDFLARE_WORKERS_API, json=payload, params=params, timeout=30)
    response.raise_for_status()
    Log.i("cpolar", f"推送结果: HTTP {response.status_code} {response.text}")


def deleteLogFiles(logPath):
    """删除 cpolar 日志的全部相关文件：符号链接本身 + 按日期滚动的真实文件 + master 日志。
    cpolar 的日志是"符号链接(logPath) -> 真实文件(logPath.YYYYMMDD)"结构，
    只删链接会留下旧日期文件，下次读到过期地址，因此按前缀整体清理。
    文件被运行中的 cpolar 占用时删除会失败，重试几次后仍失败则告警。"""
    directory = os.path.dirname(logPath) or "."
    baseName = os.path.basename(logPath)
    for attempt in range(3):
        remaining = []
        for name in os.listdir(directory):
            if name == baseName or name.startswith(baseName + "."):
                fullPath = os.path.join(directory, name)
                try:
                    os.remove(fullPath)
                    Log.i("cpolar", f"已删除旧日志 {fullPath}")
                except OSError as error:
                    remaining.append(f"{name}({error})")
                    Log.w("cpolar", f"删除旧日志 {fullPath} 失败: {error}")
        if not remaining:
            return
        time.sleep(1)
    Log.w("cpolar", f"删除旧日志失败: {remaining}")


def tailLog(logPath):
    """持续读取日志文件新增行（文件落盘不受管道缓冲影响）。
    空闲时每 0.5s yield None，供调用方检查进程退出与"未获取地址"超时。"""
    # 若文件不存在，等它出现
    while not os.path.exists(logPath):
        yield None
        time.sleep(1)
    # 从头读：启动前已删除旧日志，文件内容全部属于本次运行
    with open(logPath, "r", encoding="utf-8", errors="replace") as file:
        while True:
            line = file.readline()
            if not line:
                yield None
                time.sleep(0.5)
                continue
            stripped = line.strip()
            if stripped:
                yield stripped

if __name__ == "__main__":
    while True:
        # 启动前删除旧日志，避免 tailLog 读到上次运行的过期地址
        try:
            deleteLogFiles(CPOLAR_LOG_PATH)
        except Exception as error:
            Log.w("cpolar", f"清理旧日志异常: {error}")
        # 后台启动 cpolar，日志写文件（-log 已加进 CPOLAR_ARGS）
        process = subprocess.Popen([CPOLAR_PATH] + CPOLAR_ARGS)
        Log.i("cpolar", f"已启动 cpolar（PID={process.pid}），开始监听日志 {CPOLAR_LOG_PATH}")
        try:
            # 退出循环标志
            exitFlag = False
            # 本周期看门狗：超过该时限仍未获取到地址行则重启
            addressDeadline = time.time() + CPOLAR_RETRY_DELAY
            for line in tailLog(CPOLAR_LOG_PATH):
                if exitFlag:
                    break
                if line is None:
                    # 空闲心跳：进程退出或超时未获地址 → 重启
                    if process.poll() is not None:
                        Log.w("cpolar", f"cpolar 进程已退出（PID={process.pid}），准备重启")
                        exitFlag = True
                        break
                    if time.time() >= addressDeadline:
                        Log.w("cpolar", f"超过 {CPOLAR_RETRY_DELAY}s 未获取到隧道地址，准备重启")
                        exitFlag = True
                        break
                    continue
                # Log.i("cpolar", f"[cpolar] {line}")
                address = extractAddress(line)
                if address:
                    # 已获取地址：禁用"未获地址"超时，交给下方可用性监控
                    addressDeadline = None
                    # 一直重试推送，直到可用，指数退避策略
                    retryIndex = 1
                    while True:
                        try:
                            Log.i("cpolar", f"尝试推送地址: {address}，第 {retryIndex} 次")
                            pushAddressToCloudflareWorkers(address)
                            Log.i("cpolar", f"推送到 Cloudflare Workers 成功，地址: {address}，完整日志: {line}")
                            break
                        except Exception as error:
                            Log.e("cpolar", f"推送地址: {address} 失败: {error}")
                            retryIndex *= 2
                            time.sleep(retryIndex if retryIndex <= CF_RETRY_DELAY else CF_RETRY_DELAY)
                    # 检查服务是否可用
                    tryCount = MAX_RETRY_COUNT
                    while tryCount > 0:
                        # 检查进程是否已退出
                        if process.poll() is not None:
                            exitFlag = True
                            break
                        Log.d("cpolar", f"检查服务是否可用，第 {tryCount} 次")
                        if checkAddressAvailable(address):
                            # 若可用
                            Log.i("cpolar", f"服务已响应 {tryCount}")
                            tryCount = MAX_RETRY_COUNT
                            time.sleep(CPOLAR_RETRY_DELAY)
                        else:
                            # 若不可用
                            Log.w("cpolar", f"服务未响应 {tryCount}")
                            tryCount -= 1
                            time.sleep(1)
                        if tryCount <= 0:
                            # 若次数用完
                            Log.e("cpolar", f"服务未响应，退出")
                            exitFlag = True
                            break
                    Log.i("cpolar", f"服务已停止，重启服务")
                    # sys.exit()  # 测一次就退：抓到有效地址并推送后结束

        except Exception as error:
            Log.e("cpolar", f"监听异常: {error}")
        # 重启前确保旧进程已结束，否则会出现多个 cpolar 实例共写一个日志文件
        if process.poll() is None:
            Log.i("cpolar", f"旧 cpolar 进程（PID={process.pid}）仍在运行，先终止再重启")
            process.kill()
            process.wait()
        Log.i("cpolar", "cpolar 进程已退出，准备重启...")
        time.sleep(5)