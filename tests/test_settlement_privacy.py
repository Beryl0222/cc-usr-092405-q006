"""财政清算：只按实际合规入住夜、锁定单价、幂等、不受后续房价变化影响。"""

from station.errors import PermissionError, ReviewRequiredError
from station.models import NIGHT_STAYED, TICKET_APPROVED
from tests.world import WorldTest, event


class SettlementTest(WorldTest):
    def _staying_alloc(self, start="2026-09-22", end="2026-09-25", hotel="H01"):
        actor, app, _ = self.apply()
        alloc = self.system.confirm_booking(actor, app.id, hotel, start, end)
        self.check_in(self.front1, alloc, day=start)
        return actor, alloc

    def test_only_compliant_stayed_nights_are_settled(self):
        actor, alloc = self._staying_alloc()
        # 9/24 临时离店（当日不计补贴），9/25 归还
        self.system.ingest_event(self.front1, event(
            "lv", "front_desk", "temp_leave", "H01", alloc.bed_key,
            "2026-09-24T08:00:00+08:00", expected_return="2026-09-25"))
        self.system.ingest_event(self.front1, event(
            "rt", "door_lock", "return", "H01", alloc.bed_key,
            "2026-09-25T20:00:00+08:00"))
        self.system.ingest_event(self.front1, event(
            "co", "front_desk", "check_out", "H01", alloc.bed_key,
            "2026-09-26T10:00:00+08:00"))
        stl = self.system.settle(self.finance, alloc.id)
        dates = [l["date"] for l in stl.lines]
        # 22、23 在店；24 离店不计；25 归还当日在店
        self.assertEqual(dates, ["2026-09-22", "2026-09-23", "2026-09-25"])
        self.assertEqual(stl.total_nights, 3)
        self.assertEqual(stl.total_subsidy, 3 * 100.0)  # P2026 单价 100

    def test_subsidy_uses_locked_price_not_later_rate_changes(self):
        actor, alloc = self._staying_alloc()
        locked = alloc.nights["2026-09-22"].subsidy_locked
        # 清算前房价与政策单价都变了
        self.rates.set_range("H01", "2026-09-22", "2026-09-25", 500.0)
        self.policies.get("P2026")  # 政策对象本身冻结，验证夜线不受新价影响
        self.system.ingest_event(self.front1, event(
            "co", "front_desk", "check_out", "H01", alloc.bed_key,
            "2026-09-26T10:00:00+08:00"))
        stl = self.system.settle(self.finance, alloc.id)
        for line in stl.lines:
            self.assertEqual(line["subsidy_locked"], locked)
            self.assertEqual(line["subsidy"], locked)
        # 房价 500 的改动没有渗入清算
        self.assertNotEqual(stl.lines[0]["rate_locked"], 500.0)

    def test_settlement_is_idempotent(self):
        actor, alloc = self._staying_alloc()
        self.system.ingest_event(self.front1, event(
            "co", "front_desk", "check_out", "H01", alloc.bed_key,
            "2026-09-26T10:00:00+08:00"))
        s1 = self.system.settle(self.finance, alloc.id)
        s2 = self.system.settle(self.finance, alloc.id)
        self.assertEqual(s1.id, s2.id)
        self.assertEqual(len(self.system.settlements), 1)

    def test_settlement_blocked_by_open_ticket(self):
        actor, alloc = self._staying_alloc()
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-27")
        self.assertTrue(scan["tickets_opened"])  # 超期工单
        with self.assertRaises(ReviewRequiredError):
            self.system.settle(self.finance, alloc.id)

    def test_no_show_nights_never_settled(self):
        actor, app, _ = self.apply()
        alloc = self.system.confirm_booking(actor, app.id, "H01", "2026-09-22", "2026-09-23")
        scan = self.system.run_daily_review_scan(self.verifier, "2026-09-24")
        tid = scan["tickets_opened"][0]
        self.system.decide_ticket(self.duty, tid, TICKET_APPROVED, "确认爽约")
        stl = self.system.settle(self.finance, alloc.id)
        self.assertEqual(stl.total_nights, 0)
        self.assertEqual(stl.total_subsidy, 0.0)

    def test_finance_role_required(self):
        actor, alloc = self._staying_alloc()
        with self.assertRaises(PermissionError):
            self.system.settle(self.officer, alloc.id)

    def test_daily_subsidy_basis_view(self):
        actor, alloc = self._staying_alloc()
        basis = self.system.daily_subsidy_basis(self.finance, "H01", "2026-09-22")
        self.assertEqual(len(basis["lines"]), 1)
        line = basis["lines"][0]
        self.assertEqual(line["state"], NIGHT_STAYED)
        self.assertEqual(line["subsidy_locked"], 100.0)
        self.assertEqual(line["policy_code"], "P2026")


class MaterialPrivacyTest(WorldTest):
    def setUp(self):
        super().setUp()
        self.actor, self.app, self.material = self.apply()

    def test_officer_cannot_see_job_search_fields(self):
        """团干部处理诉求：看不到期望薪资、简历、作品集等非必要字段。"""
        view = self.system.view_material(self.officer, self.app.id)
        for key in ("expected_salary", "resume_summary", "portfolio_url",
                    "work_history", "reference_contacts", "target_positions"):
            self.assertEqual(view[key], "***无权查看***", key)
        # 但能看到服务必需字段
        self.assertEqual(view["visit_intentions"], ["某互联网企业", "某智能制造企业"])
        self.assertEqual(view["name"], "张三")
        self.assertEqual(view["phone"], "13800000001")
        # 资格字段也不对团干部开放（核验是运营岗职责）
        self.assertEqual(view["degree"], "***无权查看***")

    def test_verifier_sees_eligibility_but_not_job_search(self):
        view = self.system.view_material(self.verifier, self.app.id)
        self.assertEqual(view["degree"], "本科")
        self.assertEqual(view["household_region"], "河北省石家庄市")
        self.assertEqual(view["expected_salary"], "***无权查看***")

    def test_front_desk_sees_only_identity_minimum(self):
        view = self.system.view_material(self.front1, self.app.id)
        self.assertEqual(view["name"], "张三")
        self.assertEqual(view["id_card_masked"], "1301**********0011")
        self.assertEqual(view["phone"], "***无权查看***")
        self.assertEqual(view["graduation_date"], "***无权查看***")

    def test_finance_sees_only_identity_minimum(self):
        view = self.system.view_material(self.finance, self.app.id)
        self.assertEqual(view["name"], "张三")
        self.assertEqual(view["purpose"], "***无权查看***")

    def test_applicant_sees_everything(self):
        view = self.system.view_material(self.actor, self.app.id)
        self.assertEqual(view["expected_salary"], "8000-10000")
        self.assertEqual(view["resume_summary"], "曾在两家企业实习……")

    def test_officer_can_handle_request_without_material_access(self):
        req = self.system.create_service_request(
            self.actor, self.app.id, "enterprise_visit", "希望安排参访某互联网企业")
        handled = self.system.handle_request(self.officer, req.id, "已联系企业 HR，周四下午可安排")
        self.assertEqual(handled.status, "handling")
        self.assertEqual(handled.handler_name, "团干部·陈服务")
        # 处理诉求不赋予材料访问权
        view = self.system.view_material(self.officer, self.app.id)
        self.assertEqual(view["expected_salary"], "***无权查看***")

    def test_applicant_cannot_view_others_application(self):
        other, app2, _ = self.apply(uid="u_other2", name="李四")
        with self.assertRaises(PermissionError):
            self.system.view_material(self.actor, app2.id)


if __name__ == "__main__":
    import unittest
    unittest.main()
