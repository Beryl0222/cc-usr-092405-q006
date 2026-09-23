"""政策版本与资格规则。

关键原则：资格只按"申请时有效"的政策版本判断，申请一旦通过核验即固化快照；
之后政策调整或房价调整都不改变该申请的资格口径、最长权益天数与补贴单价。
"""

from dataclasses import dataclass, field
from datetime import date

from .errors import NotFoundError, PolicyError, ValidationError
from .timeutil import iso, parse_date

# 学历由低到高，用于"不低于某学历"类规则
DEGREE_ORDER = ["中专/高中", "大专", "本科", "硕士", "博士"]

# 来城目的：与"是否需要求职材料"挂钩
PURPOSE_JOB_SEEKING = {"求职", "面试", "参加招聘活动", "入职报到"}


@dataclass(frozen=True)
class EligibilityCriteria:
    # 户籍类型白名单（如 ["外地"]）；空元组表示不限
    household_types: tuple = ()
    # 毕业年限上限（年，含边界）；None 表示不限
    max_years_since_graduation: float = None
    # 允许学历集合；空元组表示不限
    degrees: tuple = ()
    # 允许来城目的；空元组表示不限
    purposes: tuple = ()
    # 毕业日距边界多少天内的拒绝视为"可申诉临界"，交人工复核
    boundary_appeal_days: int = 30

    def describe(self):
        return {
            "household_types": list(self.household_types),
            "max_years_since_graduation": self.max_years_since_graduation,
            "degrees": list(self.degrees),
            "purposes": list(self.purposes),
        }


@dataclass(frozen=True)
class PolicyVersion:
    code: str
    name: str
    effective_from: date
    criteria: EligibilityCriteria
    effective_to: date = None  # None 表示至今有效
    # 单次权益周期内最长免费住宿夜数
    max_free_nights: int = 30
    # 逐日财政补贴单价（元/人/夜）；仅对实际合规入住夜清算
    subsidy_per_night: float = 0.0
    # 预定起始日之后多少小时仍未到店核验即记爽约
    check_in_grace_hours: int = 18
    # 免费取消的最晚提前小时数
    free_cancel_hours: int = 24
    # 紧急延住每次最多追加夜数，且必须人工复核
    emergency_extension_nights: int = 3

    def effective_on(self, day):
        day = parse_date(day)
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def to_dict(self):
        return {
            "code": self.code,
            "name": self.name,
            "effective_from": iso(self.effective_from),
            "effective_to": iso(self.effective_to),
            "max_free_nights": self.max_free_nights,
            "subsidy_per_night": self.subsidy_per_night,
            "check_in_grace_hours": self.check_in_grace_hours,
            "free_cancel_hours": self.free_cancel_hours,
            "emergency_extension_nights": self.emergency_extension_nights,
            "criteria": self.criteria.describe(),
        }


class PolicyRegistry:
    """政策版本按生效区间管理；同一日只允许一个版本有效。"""

    def __init__(self):
        self._versions = {}

    def add(self, policy: PolicyVersion):
        for other in self._versions.values():
            if other.effective_to is None and policy.effective_to is None:
                raise ValueError(f"政策 {policy.code} 与 {other.code} 均无截止日，区间重叠")
            a_end = policy.effective_to or date(9999, 12, 31)
            b_end = other.effective_to or date(9999, 12, 31)
            if policy.effective_from <= b_end and other.effective_from <= a_end:
                raise ValueError(f"政策 {policy.code} 与 {other.code} 生效区间重叠")
        self._versions[policy.code] = policy
        return policy

    def get(self, code):
        try:
            return self._versions[code]
        except KeyError:
            raise NotFoundError(f"政策版本不存在: {code}")

    def effective_at(self, day):
        day = parse_date(day)
        matches = [p for p in self._versions.values() if p.effective_on(day)]
        if not matches:
            raise NotFoundError(f"{day} 没有有效政策版本")
        if len(matches) > 1:
            raise ValidationError(f"{day} 存在多个有效政策版本", codes=[p.code for p in matches])
        return matches[0]

    def list(self):
        return sorted(self._versions.values(), key=lambda p: p.effective_from)


REQUIRED_PROFILE_FIELDS = ("household_type", "graduation_date", "degree", "purpose")




@dataclass(frozen=True)
class EligibilityDecision:
    """资格核验通过后的固化快照，随申请保存。"""

    policy_code: str
    decided_on: date
    household_type: str
    household_region: object
    degree: str
    purpose: str
    graduation_date: date
    years_since_graduation: float
    needs_job_material: bool
    criteria: dict

    def to_dict(self):
        return {
            "policy_code": self.policy_code,
            "decided_on": iso(self.decided_on),
            "household_type": self.household_type,
            "household_region": self.household_region,
            "degree": self.degree,
            "purpose": self.purpose,
            "graduation_date": iso(self.graduation_date),
            "years_since_graduation": self.years_since_graduation,
            "needs_job_material": self.needs_job_material,
            "criteria": self.criteria,
        }


def evaluate(policy: PolicyVersion, profile, on_date):
    """按指定政策版本在 on_date 复核资格。

    返回 EligibilityDecision；不满足时抛 PolicyError（临界情形 appealable=True）。
    profile: dict，含 household_type/graduation_date/degree/purpose，
    可选 household_region（户籍所在地）。
    """
    on_date = parse_date(on_date)
    c = policy.criteria
    reasons = []
    appealable = False

    missing = [f for f in REQUIRED_PROFILE_FIELDS if not profile.get(f)]
    if missing:
        raise ValidationError("资格材料不完整", missing=missing)

    household = str(profile["household_type"]).strip()
    if c.household_types and household not in c.household_types:
        reasons.append({"field": "household_type", "actual": household,
                        "expected": list(c.household_types)})

    degree = str(profile["degree"]).strip()
    if c.degrees and degree not in c.degrees:
        reasons.append({"field": "degree", "actual": degree,
                        "expected": list(c.degrees)})

    purpose = str(profile["purpose"]).strip()
    if c.purposes and purpose not in c.purposes:
        reasons.append({"field": "purpose", "actual": purpose,
                        "expected": list(c.purposes)})

    grad = parse_date(profile["graduation_date"])
    if grad is None:
        raise ValidationError("毕业日期为空")
    if c.max_years_since_graduation is not None:
        # "毕业 N 年内"：按对日折算年数（2/29 取 2/28）
        years = _completed_years(grad, on_date)
        if years > c.max_years_since_graduation:
            reasons.append({"field": "graduation_date", "actual": iso(grad),
                            "years_since_graduation": round(years, 2),
                            "max_years": c.max_years_since_graduation})
            # 临界宽限：距年限边界不到 boundary_appeal_days 的拒绝可申诉
            if _within_boundary_appeal(grad, on_date, c):
                appealable = True
        if grad > on_date:
            reasons.append({"field": "graduation_date", "actual": iso(grad),
                            "issue": "毕业日期晚于申请日期"})

    if reasons:
        raise PolicyError(
            "资格核验未通过" if not appealable else "资格处于政策临界，需人工复核",
            reasons=reasons, appealable=appealable, policy=policy.code)

    return snapshot(policy, profile, on_date)


def snapshot(policy: PolicyVersion, profile, on_date):
    """不做拒绝判断，直接固化资格快照（人工申诉通过后使用）。"""
    on_date = parse_date(on_date)
    grad = parse_date(profile["graduation_date"])
    return EligibilityDecision(
        policy_code=policy.code,
        decided_on=on_date,
        household_type=str(profile["household_type"]).strip(),
        household_region=profile.get("household_region"),
        degree=str(profile["degree"]).strip(),
        purpose=str(profile["purpose"]).strip(),
        graduation_date=grad,
        years_since_graduation=round(_completed_years(grad, on_date), 2),
        needs_job_material=str(profile["purpose"]).strip() in PURPOSE_JOB_SEEKING,
        criteria=policy.criteria.describe(),
    )


def _completed_years(start, end):
    """两个日历日之间的精确年数（小数，按天/365.2425 折算超出整年后的零头）。"""
    whole = end.year - start.year - (
        1 if (end.month, end.day) < (start.month, start.day) else 0)
    # 整年锚点
    year_anchor = _add_years(start, whole)
    next_anchor = _add_years(start, whole + 1)
    span = (next_anchor - year_anchor).days
    into = (end - year_anchor).days
    return whole + max(0.0, into) / max(1, span)


def _add_years(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2/29
        return d.replace(year=d.year + years, day=28)


def _within_boundary_appeal(grad, on_date, criteria):
    limit = criteria.max_years_since_graduation
    boundary = _add_years(grad, int(limit) if float(limit).is_integer() else round(limit))
    over_days = (on_date - boundary).days
    return 0 < over_days <= criteria.boundary_appeal_days
