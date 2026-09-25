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

    def _applicant(self, uid, name):
        from station.models import Actor
        self.system.register_user(Actor(uid, "applicant", name))
        status, app = self.call("POST", "/api/applications", actor=uid, body={
            "material": {"name": name, "phone": "137", "id_card_masked": "110***",
                         "household_type": "外地", "graduation_date": "2025-06-30",
                         "degree": "本科", "purpose": "求职"}})
        self.assertEqual(status, 201, app)
        return app

    def test_waitlist_journey_over_http(self):
        # H02 共 3 张床：先占满，第 4 人登记候补
        guests = []
        for i in range(3):
            app = self._applicant(f"u_hg{i}", f"住客{i}")
            status, alloc = self.call(
                "POST", f"/api/applications/{app['id']}/bookings",
                actor=f"u_hg{i}",
                body={"hotel_code": "H02", "start": "2026-09-23",
                      "end": "2026-09-24"})
            self.assertEqual(status, 201, alloc)
            guests.append((f"u_hg{i}", alloc))

        wapp = self._applicant("u_hwait", "候补客")
        status, entry = self.call("POST", "/api/waitlist", actor="u_hwait", body={
            "app_id": wapp["id"], "start": "2026-09-23", "end": "2026-09-24",
            "preferred_hotels": ["H02"], "urgency": "recruit"})
        self.assertEqual(status, 201, entry)
        self.assertEqual(entry["status"], "queued")
        self.assertEqual(entry["queue_position"], 1)
        wait_id = entry["id"]

        # 工作人员可见可解释队列；申请人只见本人；不能看全员队列
        status, staff = self.call("GET", "/api/waitlist", actor="u_verifier")
        self.assertEqual(status, 200)
        self.assertEqual(staff["queue"][0]["rank_reasons"]["urgency"], "recruit")
        status, mine = self.call("GET", "/api/waitlist/mine", actor="u_hwait")
        self.assertEqual([q["id"] for q in mine["queue"]], [wait_id])
        status, denied = self.call("GET", "/api/waitlist", actor="u_hwait")
        self.assertEqual(status, 403)

        # 一位住客退订 → 候补拿到暂时保留
        status, _ = self.call("POST", f"/api/allocations/{guests[0][1]['id']}/cancel",
                              actor=guests[0][0], body={})
        self.assertEqual(status, 200)
        status, offered = self.call("GET", f"/api/waitlist/{wait_id}",
                                    actor="u_hwait")
        self.assertEqual(offered["status"], "offered")
        self.assertEqual(offered["offer"]["hotel_code"], "H02")
        self.assertIsNotNone(offered["offer"]["confirm_deadline"])
        token = offered["offer"]["offer_token"]

        # 确认 → 占房落地；离线重复确认幂等、只扣一次权益
        status, confirmed = self.call(
            "POST", f"/api/waitlist/{wait_id}/confirm", actor="u_hwait",
            body={"token": token})
        self.assertEqual(status, 200, confirmed)
        self.assertFalse(confirmed["idempotent"])
        self.assertEqual(confirmed["allocation"]["hotel_code"], "H02")
        alloc_id = confirmed["allocation"]["id"]
        status, again = self.call(
            "POST", f"/api/waitlist/{wait_id}/confirm", actor="u_hwait",
            body={"token": token})
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["allocation"]["id"], alloc_id)
        status, ent = self.call("GET", "/api/entitlements/u_hwait", actor="u_hwait")
        self.assertEqual(ent["used_nights"], 2)

        # 前台房态板可见新占房
        status, board = self.call(
            "GET", "/api/hotels/H02/board?date=2026-09-23", actor="u_front_h02")
        rows = [r for r in board["beds"] if r["allocation_id"] == alloc_id]
        self.assertEqual(len(rows), 1)

    def test_waitlist_expiration_endpoint_requires_staff(self):
        # 申请人无权驱动截止扫描；运营核验可以
        app = self._applicant("u_exp_http", "截止客")
        status, _ = self.call("POST", "/api/waitlist/expirations",
                              actor="u_exp_http", body={})
        self.assertEqual(status, 403)
        status, payload = self.call("POST", "/api/waitlist/expirations",
                                    actor="u_verifier", body={})
        self.assertEqual(status, 200)
        self.assertIn("expired", payload)


if __name__ == "__main__":
    unittest.main()
