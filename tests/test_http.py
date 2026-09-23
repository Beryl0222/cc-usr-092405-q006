"""HTTP 集成测试：真实 socket 上验证路由、身份、错误映射与 JSON 形状。"""

import json
import threading
import unittest
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from station.seed import build_seed_system
from station.timeutil import CST
from tests.world import make_clock


class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.clock = make_clock(datetime(2026, 9, 22, 9, 0, tzinfo=CST))
        cls.system = build_seed_system(cls.clock)
        service.set_system(cls.system)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.set_system(None)

    def call(self, method, path, actor=None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor-Id", actor)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))


class HttpApiTest(HttpTestBase):
    def test_health_still_works(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "youth-lodging")

    def test_policies_listed(self):
        status, payload = self.call("GET", "/api/policies", actor="u_duty")
        self.assertEqual(status, 200)
        self.assertEqual([p["code"] for p in payload["policies"]], ["P2025", "P2026"])

    def test_hotels_have_28_sites(self):
        status, payload = self.call("GET", "/api/hotels", actor="u_duty")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["hotels"]), 28)

    def test_missing_actor_rejected(self):
        status, payload = self.call("GET", "/api/hotels")
        self.assertEqual(status, 403)

    def test_unknown_route_404(self):
        status, _ = self.call("GET", "/api/nope", actor="u_duty")
        self.assertEqual(status, 404)

    def test_full_journey_over_http(self):
        from station.models import Actor
        self.system.register_user(Actor("u_http", "applicant", "HTTP 客"))
        # 1) 提交申请
        status, app = self.call("POST", "/api/applications", actor="u_http", body={
            "material": {
                "name": "HTTP 客", "phone": "137", "id_card_masked": "110***",
                "household_type": "外地", "graduation_date": "2025-06-30",
                "degree": "本科", "purpose": "求职",
                "expected_salary": "9000", "visit_intentions": ["某企业"]}})
        self.assertEqual(status, 201, app)
        self.assertEqual(app["status"], "eligible")
        app_id = app["id"]

        # 2) 确认占房
        status, alloc = self.call("POST", f"/api/applications/{app_id}/bookings",
                                  actor="u_http",
                                  body={"hotel_code": "H01", "start": "2026-09-22",
                                        "end": "2026-09-24"})
        self.assertEqual(status, 201, alloc)
        alloc_id = alloc["id"]
        bed = alloc["bed_key"]

        # 3) 前台补传入住（断网场景：recorded 远晚于 occurred）
        status, result = self.call("POST", "/api/events", actor="u_front_h01", body={
            "event_id": "http_e1", "source": "door_lock", "event_type": "check_in",
            "hotel_code": "H01", "bed_key": bed,
            "occurred_at": "2026-09-22T14:00:00+08:00",
            "recorded_at": "2026-09-22T19:00:00+08:00", "offline": True})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["result"]["status"], "applied")
        # 重复补传 → duplicate
        status, dup = self.call("POST", "/api/events", actor="u_front_h01", body={
            "event_id": "http_e1", "source": "door_lock", "event_type": "check_in",
            "hotel_code": "H01", "bed_key": bed,
            "occurred_at": "2026-09-22T14:00:00+08:00"})
        self.assertEqual(dup["result"]["status"], "duplicate")

        # 4) 撤回未使用日期
        status, wd = self.call("POST", f"/api/allocations/{alloc_id}/withdraw",
                               actor="u_http", body={"dates": ["2026-09-24"]})
        self.assertEqual(status, 200, wd)
        self.assertEqual(wd["released"], ["2026-09-24"])

        # 5) 权益视图
        status, ent = self.call("GET", f"/api/entitlements/u_http", actor="u_http")
        self.assertEqual(ent["remaining_nights"], 28)

        # 6) 退房 + 财政清算
        status, co = self.call("POST", "/api/events", actor="u_front_h01", body={
            "event_id": "http_e2", "source": "front_desk", "event_type": "check_out",
            "hotel_code": "H01", "bed_key": bed,
            "occurred_at": "2026-09-24T10:00:00+08:00"})
        self.assertEqual(co["result"]["status"], "applied")
        status, stl = self.call("POST", f"/api/allocations/{alloc_id}/settle",
                                actor="u_finance", body={})
        self.assertEqual(status, 200, stl)
        self.assertEqual(stl["total_nights"], 2)
        self.assertEqual(stl["total_subsidy"], 200.0)

        # 7) 团干部看材料被脱敏
        status, view = self.call("GET", f"/api/applications/{app_id}/material",
                                 actor="u_officer")
        self.assertEqual(view["material"]["expected_salary"], "***无权查看***")
        self.assertEqual(view["material"]["visit_intentions"], ["某企业"])

    def test_error_shape_stable(self):
        status, payload = self.call("POST", "/api/applications", actor="u_duty",
                                    body={"material": {}})
        self.assertEqual(status, 403)
        self.assertIn("code", payload["error"])
        self.assertIn("message", payload["error"])

    def test_review_required_maps_to_202(self):
        from station.models import Actor
        self.system.register_user(Actor("u_edge_http", "applicant", "临界"))
        status, payload = self.call("POST", "/api/applications",
                                    actor="u_edge_http", body={
                "material": {"name": "临界", "household_type": "外地",
                             "graduation_date": "2023-09-12", "degree": "本科",
                             "purpose": "求职"}})
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["error"]["code"], "REVIEW_REQUIRED")
        self.assertTrue(payload["error"]["ticket_id"])


if __name__ == "__main__":
    unittest.main()
