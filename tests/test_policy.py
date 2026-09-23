"""政策版本生效区间与资格快照测试。"""

import unittest
from datetime import date

from station.errors import NotFoundError, PolicyError, ValidationError
from station.policy import (EligibilityCriteria, PolicyRegistry, PolicyVersion,
                            evaluate)

P = lambda code, start, criteria, end=None: PolicyVersion(
    code, code, start, criteria, effective_to=end,
    max_free_nights=30, subsidy_per_night=80)


class PolicyRegistryTest(unittest.TestCase):
    def test_effective_at_picks_version_for_date(self):
        reg = PolicyRegistry()
        c = EligibilityCriteria()
        reg.add(P("V1", date(2025, 1, 1), c, date(2025, 12, 31)))
        reg.add(P("V2", date(2026, 1, 1), c))
        self.assertEqual(reg.effective_at("2025-06-01").code, "V1")
        self.assertEqual(reg.effective_at("2025-12-31").code, "V1")
        self.assertEqual(reg.effective_at("2026-01-01").code, "V2")
        self.assertEqual(reg.effective_at("2030-01-01").code, "V2")

    def test_overlapping_versions_rejected(self):
        reg = PolicyRegistry()
        reg.add(P("V1", date(2025, 1, 1), EligibilityCriteria(), date(2025, 12, 31)))
        with self.assertRaises(ValueError):
            reg.add(P("V2", date(2025, 12, 1), EligibilityCriteria()))

    def test_no_effective_version(self):
        reg = PolicyRegistry()
        reg.add(P("V1", date(2026, 6, 1), EligibilityCriteria()))
        with self.assertRaises(NotFoundError):
            reg.effective_at("2026-01-01")


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.policy = P("V", date(2026, 1, 1), EligibilityCriteria(
            household_types=("外地",), max_years_since_graduation=2,
            degrees=("本科", "硕士"), purposes=("求职", "面试")))

    def profile(self, **kw):
        base = {"household_type": "外地", "graduation_date": "2025-06-30",
                "degree": "本科", "purpose": "求职"}
        base.update(kw)
        return base

    def test_pass_freezes_snapshot(self):
        d = evaluate(self.policy, self.profile(), date(2026, 9, 22))
        self.assertEqual(d.policy_code, "V")
        self.assertTrue(d.needs_job_material)
        self.assertAlmostEqual(d.years_since_graduation, 1.23, places=1)
        self.assertEqual(d.criteria["household_types"], ["外地"])

    def test_local_household_denied(self):
        with self.assertRaises(PolicyError) as cm:
            evaluate(self.policy, self.profile(household_type="本地"), date(2026, 9, 22))
        self.assertFalse(cm.exception.appealable)
        self.assertEqual(cm.exception.reasons[0]["field"], "household_type")

    def test_degree_denied(self):
        with self.assertRaises(PolicyError) as cm:
            evaluate(self.policy, self.profile(degree="大专"), date(2026, 9, 22))
        self.assertEqual(cm.exception.reasons[0]["field"], "degree")

    def test_purpose_denied(self):
        with self.assertRaises(PolicyError) as cm:
            evaluate(self.policy, self.profile(purpose="旅游"), date(2026, 9, 22))
        self.assertEqual(cm.exception.reasons[0]["field"], "purpose")

    def test_graduation_limit_boundary_is_inclusive(self):
        # 2024-09-22 毕业，2026-09-22 申请：正好 2 年，应通过
        d = evaluate(self.policy, self.profile(graduation_date="2024-09-22"),
                     date(2026, 9, 22))
        self.assertEqual(d.policy_code, "V")

    def test_graduation_over_limit_hard_denied_far_from_boundary(self):
        with self.assertRaises(PolicyError) as cm:
            evaluate(self.policy, self.profile(graduation_date="2023-01-01"),
                     date(2026, 9, 22))
        self.assertFalse(cm.exception.appealable)

    def test_graduation_within_appeal_window_is_reviewable(self):
        # 超出年限 20 天（默认临界窗口 30 天）：可申诉
        with self.assertRaises(PolicyError) as cm:
            evaluate(self.policy, self.profile(graduation_date="2024-09-02"),
                     date(2026, 9, 22))
        self.assertTrue(cm.exception.appealable)

    def test_missing_fields(self):
        with self.assertRaises(ValidationError):
            evaluate(self.policy, {"household_type": "外地"}, date(2026, 9, 22))

    def test_leap_day_graduation(self):
        pol = P("V", date(2020, 1, 1), EligibilityCriteria(
            max_years_since_graduation=2))
        d = evaluate(pol, self.profile(graduation_date="2024-02-29"),
                     date(2026, 2, 28))
        self.assertEqual(d.policy_code, "V")


if __name__ == "__main__":
    unittest.main()
