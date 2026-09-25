"""青年驿站运营的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = "youth-lodging"
SERVICE_NAME = "青年驿站运营"

_SYSTEM = None
_API = None
_RESTORE_PATH = None


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def get_system():
    """惰性构建种子系统，便于测试替换。"""
    global _SYSTEM, _API
    if _SYSTEM is None:
        from station.seed import build_seed_system
        from station.http_api import Api
        from station.persistence import restore_system
        _SYSTEM = build_seed_system()
        if _RESTORE_PATH:
            import json
            with open(_RESTORE_PATH, encoding="utf-8") as fh:
                restore_system(_SYSTEM, json.load(fh))
        _API = Api(_SYSTEM)
    return _SYSTEM, _API


def set_system(system):
    """测试注入自定义系统。"""
    global _SYSTEM, _API
    from station.http_api import Api
    _SYSTEM = system
    _API = Api(system) if system is not None else None


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 青年驿站业务 API。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, health_payload())
            return
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        _, api = get_system()
        body = self._read_body() if method == "POST" else {}
        if body is None:
            self._send_json(400, {"error": {"code": "BAD_JSON",
                                            "message": "请求体不是合法 JSON"}})
            return
        try:
            status, payload = api.dispatch(
                method, self.path,
                {k.lower(): v for k, v in self.headers.items()}, body)
        except Exception as exc:  # 领域异常 → 稳定错误体
            from station.http_api import error_payload
            status, payload = error_payload(exc)
        self._send_json(status, payload)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--now", help="注入固定时钟（ISO 时间），用于自动化场景")
    parser.add_argument("--snapshot", help="启动后把系统快照写入该 JSON 路径并退出")
    parser.add_argument("--restore", help="从该 JSON 快照恢复后再启动")
    args = parser.parse_args()

    global _RESTORE_PATH
    _RESTORE_PATH = args.restore

    if args.now:
        from station.timeutil import parse_dt
        fixed = parse_dt(args.now)
        from station import seed as seed_mod
        _orig_build = seed_mod.build_seed_system

        def build_with_clock(now_fn=None):  # noqa: ANN001
            return _orig_build(now_fn or (lambda: fixed))

        seed_mod.build_seed_system = build_with_clock

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        system, _ = get_system()
        from station.seed import check_seed
        info = check_seed(system)
        print(f"基础检查通过：{info['hotels']} 处酒店、{info['beds']} 张床位、"
              f"政策版本 {','.join(info['policies'])}")
        return

    system, _ = get_system()
    if args.snapshot:
        from station.persistence import dump_json
        dump_json(system, args.snapshot)
        print(f"快照已写入 {args.snapshot}")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
