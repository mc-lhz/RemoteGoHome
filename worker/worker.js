// 远程回家 - Cloudflare Worker
// 作为 KV 读写代理：家庭端通过它写入 cpolar 地址，远程端通过它读取。
// 只暴露一个共享密码，KV 凭据不外泄给客户端。

// 默认端口与密码均来自环境变量（见 wrangler.toml 的 vars 与 secret）。
function jsonify(data) {
  return new Response(JSON.stringify(data), {
    status: data.code || 200,
    headers: { "Content-Type": "application/json" },
  });
}

// 通过查询参数 password 验证，env 与 url 必须作为参数传入
function checkAuth(url, env) {
  const expectPassword = env.PASSWORD || "";
  const inputPassword = url.searchParams.get("password") || "";
  return inputPassword === expectPassword;
}

// 读取 KV 中的隧道地址，env 必须作为参数传入
async function readKV(localPort, env) {
  return await env.REMOTE_KV.get(localPort);
}

export default {
  async fetch(request, env) {
    const workersUrl = new URL(request.url);
    const urlRoute = workersUrl.pathname;
    // 默认端口取自环境变量，未配置时兜底 80
    const defaultPort = env.DEFAULT_PORT || "80";

    if (!checkAuth(workersUrl, env)) {
      return jsonify({ code: 401, msg: "UnAuthorized" });
    }

    // 写入新地址：POST /write?port=80&password=xxx  body: {"url": "..."}
    if (urlRoute === "/write") {
      const localPort = workersUrl.searchParams.get("port") || defaultPort;
      try {
        const body = await request.json();
        const tunnelUrl = body.url;
        if (!tunnelUrl) {
          return jsonify({ code: 400, msg: "url required" });
        }
        // 永久不过期，故不传 expirationTtl
        await env.REMOTE_KV.put(localPort, tunnelUrl);
        return jsonify({ code: 200, msg: "success", port: localPort, url: tunnelUrl });
      } catch (error) {
        return jsonify({ code: 400, msg: error.message });
      }
    }

    // 读取地址：GET /read?port=80&password=xxx
    if (urlRoute === "/read") {
      const localPort = workersUrl.searchParams.get("port") || defaultPort;
      const tunnelUrl = await readKV(localPort, env);
      return jsonify({ code: 200, msg: "success", port: localPort, url: tunnelUrl });
    }

    // 跳转到隧道地址：GET /redirect?port=80&password=xxx
    if (urlRoute === "/redirect") {
      const localPort = workersUrl.searchParams.get("port") || defaultPort;
      const tunnelUrl = await readKV(localPort, env);
      if (!tunnelUrl) {
        return jsonify({ code: 404, msg: "no address" });
      }
      return new Response(
        "<script>window.location.href='" + tunnelUrl + "';</script>",
        { headers: { "Content-Type": "text/html" } }
      );
    }

    return jsonify({ code: 404, msg: "Not Found" });
  },
};
