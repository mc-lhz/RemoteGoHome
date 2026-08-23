import re
import time
import os
import sys
import subprocess
import requests
from dotenv import loadDotenv
import Logcat

loadDotenv()

Log = Logcat.Logcat(outputFile=None)

# ======= 配置（从环境变量读取，见 .env）=======
CPOLAR_PATH = os.environ.get("CPOLAR_PATH")
CPOLAR_FILENAME = os.path.basename(CPOLAR_PATH)
PORT = int(os.environ.get("PORT", "80"))
# cpolar 把日志落盘，Python 读文件抓取地址（已验证有效）
CPOLAR_LOG_PATH = os.environ.get("CPOLAR_LOG_PATH")
CPOLAR_ARGS = ["http", str(PORT), "-log=" + CPOLAR_LOG_PATH]
MAX_RETRY_COUNT = 5

CLOUDFLARE_WORKERS_API = "https://remote.mclhz.de5.net/write"
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
    仅当明确失效（404：cpolar 窗口/隧道已挂）才返回 False；
    其他一切情况（200、5xx、超时、连接异常等）一律返回 True（不当作失败），
    异常在此吞掉，避免冒泡导致误杀进程。"""
    try:
        response = requests.get(address, timeout=10)
        # Log.d("cpolar", f"检查地址: {address}，状态码: {response.status_code}，响应内容: {response.text}")
        if response.status_code == 404:
            Log.d("cpolar", f"地址 {address} 已失效")
            return False
        return True
    except Exception:
        return True


def pushAddressToCloudflareWorkers(address):
    """将地址推送到 Cloudflare Workers（带 password 查询参数鉴权）。"""
    payload = {
        "port": PORT,
        "url": address,
    }
    params = {"password": WORKER_PASSWORD}
    response = requests.post(CLOUDFLARE_WORKERS_API, json=payload, params=params, timeout=30)
    Log.i("cpolar", f"推送结果: HTTP {response.status_code} {response.text}")


def tailLog(logPath):
    """持续读取日志文件新增行（文件落盘不受管道缓冲影响）。"""
    # 若文件不存在，等它出现
    while not os.path.exists(logPath):
        time.sleep(1)
    with open(logPath, "r", encoding="utf-8", errors="replace") as file:
        file.seek(0, os.SEEK_END)
        while True:
            line = file.readline()
            if not line:
                time.sleep(0.5)
                continue
            yield line.strip()





if __name__ == "__main__":
    while True:
        # 后台启动 cpolar，日志写文件（-log 已加进 CPOLAR_ARGS）
        process = subprocess.Popen([CPOLAR_PATH] + CPOLAR_ARGS)
        Log.i("cpolar", f"已启动 cpolar（PID={process.pid}），开始监听日志 {CPOLAR_LOG_PATH}")
        try:
            exitFlag = False
            for line in tailLog(CPOLAR_LOG_PATH):
                if exitFlag:
                    break
                # Log.i("cpolar", f"[cpolar] {line}")
                address = extractAddress(line)
                if address:
                    Log.i("cpolar", f"检测到新地址: {address}，完整日志: {line}")
                    try:
                        pushAddressToCloudflareWorkers(address)
                    except Exception as error:
                        Log.e("cpolar", f"推送地址: {address} 失败: {error}")
                    tryCount = MAX_RETRY_COUNT
                    while tryCount > 0:
                        # 检查进程是否已退出
                        if process.poll() is not None:
                            exitFlag = True
                            break
                        if checkAddressAvailable(address):
                            # 若可用
                            tryCount = MAX_RETRY_COUNT
                            time.sleep(20)
                        else:
                            # 若不可用
                            Log.i("cpolar", f"服务未响应 {tryCount}")
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
        Log.i("cpolar", "cpolar 进程已退出，准备重启...")
        time.sleep(5)