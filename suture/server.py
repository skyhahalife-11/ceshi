"""图形界面用的本地服务。

只监听 127.0.0.1，端口由系统随机分配，并且每次启动生成一个一次性令牌——
这个接口能读到配置里的 Key，不能让本机上其它程序随便调用。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import engine as E

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


class _State:
    def __init__(self, engine: E.Engine):
        self.engine = engine
        self.token = secrets.token_urlsafe(24)
        self.last_report: Optional[E.Report] = None
        self.lock = threading.Lock()


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    state: _State = None       # 由 create_server 绑定

    def log_message(self, fmt, *args):
        pass

    # ---- 鉴权 ----
    def _authorized(self) -> bool:
        header = self.headers.get("X-Suture-Token")
        if header and secrets.compare_digest(header, self.state.token):
            return True
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            for part in query.split("&"):
                if part.startswith("token="):
                    return secrets.compare_digest(part[len("token="):], self.state.token)
        return False

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            if not self._authorized():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
                html = f.read().replace("__SUTURE_TOKEN__", self.state.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            if not self._authorized():
                self._send_json({"error": "forbidden"}, 403)
                return
            eng = self.state.engine
            self._send_json({
                "profile_source": eng.profile_source,
                "gateway_name": eng.profile.get("gateway_name", "AI Gate"),
                "model_count": len(eng.profile.get("models", [])),
                "installed": [{"id": a.harness_id, "name": a.display_name}
                              for a in eng.installed()],
            })
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            self._send_json({"error": "forbidden"}, 403)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}

        with self.state.lock:
            if path == "/api/check":
                report = self.state.engine.run(harness_ids=payload.get("harness_ids"))
                self.state.last_report = report
                self._send_json(E.report_to_dict(report))
                return
            if path == "/api/fix":
                harness_id = payload.get("harness_id")
                report = self.state.last_report
                if not report:
                    self._send_json({"error": "还没有检查结果，先执行检查"}, 400)
                    return
                target = next((h for h in report.harnesses if h.harness_id == harness_id), None)
                if target is None:
                    self._send_json({"error": f"没有 {harness_id} 的检查结果"}, 400)
                    return
                self._send_json(self.state.engine.fix(harness_id, target.findings))
                return
            if path == "/api/apply_choice":
                harness_id = payload.get("harness_id")
                field = payload.get("field")
                value = payload.get("value")
                if not harness_id or not field or value is None:
                    self._send_json({"error": "缺少 harness_id/field/value"}, 400)
                    return
                self._send_json(self.state.engine.apply_choice(harness_id, field, value))
                return
            if path == "/api/generate":
                harness_id = payload.get("harness_id")
                try:
                    generated = self.state.engine.generate_config(harness_id)
                except Exception as exc:      # noqa: BLE001 —— 生成失败要如实回报
                    self._send_json({"error": f"生成配置失败：{exc}"}, 500)
                    return
                self._send_json({"path": generated})
                return
        self._send_json({"error": "not found"}, 404)


def create_server(engine: E.Engine, port: int = 0):
    state = _State(engine)
    handler_cls = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    return httpd, state


def serve_in_background(engine: E.Engine, port: int = 0):
    httpd, state = create_server(engine, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={state.token}"
    return httpd, state, url
