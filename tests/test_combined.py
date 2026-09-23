"""综合场景：政策生效日跨越、两店争抢最后床位、离线入住补传同时发生。

系统必须同时给出：唯一房态、逐日补贴依据、剩余权益、人工例外责任人。
"""

import threading
from datetime import datetime

from station.errors import DomainError
from station.models import NIGHT_STAYED
from station.timeutil import CST
from tests.world import WorldTest, applicant, event


class CombinedStressTest(WorldTest):
    def test_policy_crossing_last_bed_and_offline_checkin(self):
        # ---- 政策生效日跨越：2025-12-31 提交用 P2025，2026-01-01 提交用 P2026 ----
        self.clock.set(datetime(2025, 12, 31, 10, 0, tzinfo=CST))
        a_old, m_old = applicant(self.system, "u_old", "跨年甲",
                                 graduation_date="2024-06-30", degree="大专")
        app_old = self.system.submit_application(a_old, m_old)
        self.assertEqual(app_old.policy_code, "P2025")

        self.clock.set(datetime(2026, 1, 1, 10, 0, tzinfo=CST))
        a_new, m_new = applicant(self.system, "u_new", "跨年乙",
                                 graduation_date="2024-06-30", degree="本科")
        app_new = self.system.submit_application(a_new, m_new)
        self.assertEqual(app_new.policy_code, "P2026")
        # 同一份材料在 2026 版下大专不再合格——但跨年甲的资格快照不受影响
        self.assertEqual(app_old.eligibility["degree"], "大专")

        # 跨年甲的占房横跨政策生效日：每晚仍按 P2025 单价锁定
        alloc_old = self.system.confirm_booking(
            a_old, app_old.id, "H01", "2025-12-30", "2026-01-03")
        self.assertEqual(alloc_old.nights["2025-12-31"].subsidy_locked, 80.0)
        self.assertEqual(alloc_old.nights["2026-01-01"].subsidy_locked, 80.0)
        self.assertEqual(alloc_old.nights["2026-01-01"].policy_code, "P2025")

        # ---- 两店/两人争抢 H02 最后床位（与离线补传并发）----
        a_r1, m_r1 = applicant(self.system, "u_race1", "抢床甲",
                               graduation_date="2024-06-30")
        a_r2, m_r2 = applicant(self.system, "u_race2", "抢床乙",
                               graduation_date="2024-06-30")
        app_r1 = self.system.submit_application(a_r1, m_r1)
        app_r2 = self.system.submit_application(a_r2, m_r2)

        # 抢床甲已确认 H01 占房，其前台断网，入住记录稍后补传
        alloc_r1 = self.system.confirm_booking(a_r1, app_r1.id, "H01",
                                               "2026-01-01", "2026-01-05")
        offline_checkin = event(
            "off_ci_1", "door_lock", "check_in", "H01", alloc_r1.bed_key,
            "2026-01-01T14:05:00+08:00",
            recorded_at="2026-01-01T22:30:00+08:00", offline=True)

        results = {}

        def race(who, app, key):
            try:
                alloc = self.system.confirm_booking(
                    who, app.id, "H02", "2026-01-01", "2026-01-03")
                results[key] = ("ok", alloc.id)
            except DomainError as exc:
                results[key] = (exc.code, None)

        def backfill():
            results["backfill"] = self.system.ingest_event(self.front1, offline_checkin)

        threads = [
            threading.Thread(target=race, args=(a_r2, app_r2, "race2")),
            threading.Thread(target=race, args=(a_new, app_new, "race_new")),
            threading.Thread(target=backfill),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 唯一房态：H02 最后床位恰好一个赢家
        winners = [k for k in ("race2", "race_new") if results[k][0] == "ok"]
        self.assertEqual(len(winners), 1)
        loser = "race_new" if winners[0] == "race2" else "race2"
        self.assertEqual(results[loser][0], "ROOM_UNAVAILABLE")
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            self.assertIn(("H02", "301-01", day), self.system._bed_night)
        owners = {self.system._bed_night[("H02", "301-01", d)]
                  for d in ("2026-01-01", "2026-01-02", "2026-01-03")}
        self.assertEqual(len(owners), 1)

        # 离线补传成功落地且幂等
        self.assertEqual(results["backfill"]["result"]["status"], "applied")
        again = self.system.ingest_event(self.front1, offline_checkin)
        self.assertEqual(again["result"]["status"], "duplicate")
        self.assertEqual(alloc_r1.nights["2026-01-01"].state, NIGHT_STAYED)

        # 逐日补贴依据：跨年甲 1/1 仍按 P2025 的 80 元
        basis = self.system.daily_subsidy_basis(self.finance, "H01", "2026-01-01")
        line_old = next(l for l in basis["lines"]
                        if l["allocation_id"] == alloc_old.id)
        self.assertEqual(line_old["subsidy_locked"], 80.0)
        self.assertEqual(line_old["policy_code"], "P2025")

        # 剩余权益：跨年甲 5 夜、抢床甲 5 夜
        self.assertEqual(self.system.entitlement("u_old")["remaining_nights"], 25)
        self.assertEqual(self.system.entitlement("u_race1")["remaining_nights"], 25)

        # 人工例外责任人：抢床甲 1/6 仍未退房 → 超期工单挂在值班长名下
        self.clock.set(datetime(2026, 1, 7, 9, 0, tzinfo=CST))
        scan = self.system.run_daily_review_scan(self.verifier, "2026-01-07")
        overstay = [self.system.tickets[t] for t in scan["tickets_opened"]
                    if self.system.tickets[t].type == "overstay"]
        self.assertTrue(overstay)
        for ticket in overstay:
            self.assertEqual(ticket.owner_id, "u_duty")
            self.assertEqual(ticket.owner_name, "值班长·马倩")


if __name__ == "__main__":
    import unittest
    unittest.main()
