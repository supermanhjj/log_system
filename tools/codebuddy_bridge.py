#!/usr/bin/env python3
"""WorkBuddy / CodeBuddy CLI 宿主机桥接（开发期用，零第三方依赖）。

背景：ai-service 跑在 Linux 容器里且镜像内没有 Node 运行时，而 codebuddy 是
macOS 上的 Node.js CLI；容器无法直接执行 CLI 会话。本脚本在宿主机把
``codebuddy -p --model <id>`` 包成一个只监听 127.0.0.1 的 HTTP 端点，
供 ai-service 经 ``host.docker.internal`` 调用（见 compose.override.yml）。

启动（宿主机，需先完成一次交互式登录）::

    codebuddy /login
    python3 tools/codebuddy_bridge.py            # 监听 127.0.0.1:8010

可用环境变量：CODEBUDDY_CLI（CLI 路径）、CLI_BRIDGE_PORT（监听端口）。

安全边界：仅绑定回环地址、只接受 POST /invoke、参数以 list 形式传给
subprocess（不经 shell）；不读取或返回任何凭据；不开放到局域网。
"""
import json
import os
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLI = os.getenv("CODEBUDDY_CLI", "/opt/homebrew/bin/codebuddy")
PORT = int(os.getenv("CLI_BRIDGE_PORT", "8010"))
LOGIN_HINT = "请在宿主机执行 codebuddy /login 完成交互式登录后重试"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def invoke(model: str, prompt: str, timeout: int) -> dict:
    started = time.time()
    # codebuddy -p 会占用一个本地端口，显式指定空闲端口避免 EADDRINUSE
    cmd = [CLI, "-p", "--model", model, "--output-format", "text", "-y",
           "--port", str(free_port())]
    try:
        cp = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=timeout, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到 CLI：{CLI}",
                "hint": "设置 CODEBUDDY_CLI 为宿主机真实路径"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"CLI 执行超过 {timeout}s", "hint": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "hint": ""}

    text = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()
    result = {
        "ok": cp.returncode == 0 and bool(text),
        "text": text, "returncode": cp.returncode,
        "stderr": stderr[:600], "elapsedMs": int((time.time() - started) * 1000),
    }
    if not result["ok"]:
        # 实测：未登录时 returncode=0、stdout 为空、stderr 提示认证
        result["error"] = "CLI 未返回内容" if cp.returncode == 0 else f"CLI 返回码 {cp.returncode}"
        result["hint"] = LOGIN_HINT if "Authentication required" in stderr or "/login" in stderr else \
            "确认所选模型 ID 可用、CLI 已登录；可先在宿主机手动执行 codebuddy -p --model <id>"
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True, "cli": CLI,
                            "cliExists": os.path.isfile(CLI), "port": PORT})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/invoke":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"ok": False, "error": f"请求体不是合法 JSON：{e}"})
        model = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "")
        try:
            timeout = min(max(int(payload.get("timeout") or 60), 5), 120)
        except (TypeError, ValueError):
            timeout = 60
        if not model or not prompt.strip():
            return self._send(400, {"ok": False, "error": "model 与 prompt 均不能为空"})
        print(f"[bridge] model={model} prompt_len={len(prompt)} timeout={timeout}s", flush=True)
        self._send(200, invoke(model, prompt, timeout))

    def log_message(self, fmt: str, *args) -> None:
        print(f"[bridge] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[bridge] CLI={CLI} (exists={os.path.isfile(CLI)}) listening 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
