"""测试夹具：固定时钟下构造双政策、28 店中两店的小型世界。"""

from datetime import date, datetime
import unittest

from station.catalog import Hotel, HotelDirectory, RateCalendar, make_beds
from station.models import Actor
from station.policy import (EligibilityCriteria, PolicyRegistry, PolicyVersion)
from station.system import LodgingSystem
from station.timeutil import CST

# 固定"今天"，整个测试套件不依赖真实墙钟
TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=CST)


def make_clock(initial=NOW):
    """可变时钟：测试里可推进时间。"""
    current = {"t": initial}

    def now_fn():
        return current["t"]

    def advance(**kwargs):
        from datetime import timedelta
        current["t"] += timedelta(**kwargs)

    def set_(dt):
        current["t"] = dt

    now_fn.advance = advance
    now_fn.set = set_
    return now_fn


def build_policies(subsidy_v1=80.0, subsidy_v2=100.0):
    """两版政策：2025 版与 2026 版在 2026-01-01 切换，用于验证生效日跨越。"""
    reg = PolicyRegistry()
    reg.add(PolicyVersion(
        "P2025", "2025 年度青年驿站办法", date(2025, 1, 1),
        EligibilityCriteria(
            household_types=("外地",),
            max_years_since_graduation=2,
            degrees=("大专", "本科", "硕士", "博士"),
            purposes=("求职", "面试", "参加招聘活动", "入职报到")),
        effective_to=date(2025, 12, 31),
        max_free_nights=30, subsidy_per_night=subsidy_v1,
        check_in_grace_hours=18, free_cancel_hours=24,
        emergency_extension_nights=3))
    reg.add(PolicyVersion(
        "P2026", "2026 年度青年驿站办法", date(2026, 1, 1),
        EligibilityCriteria(
            household_types=("外地",),
            max_years_since_graduation=3,
            degrees=("本科", "硕士", "博士"),          # 2026 起大专不再符合
            purposes=("求职", "面试", "参加招聘活动", "入职报到", "创业考察")),
        max_free_nights=30, subsidy_per_night=subsidy_v2,
        check_in_grace_hours=18, free_cancel_hours=24,
        emergency_extension_nights=3))
    return reg


def build_directory():
    directory = HotelDirectory()
    # 每店至少两床，便于争抢最后床位场景
    h1 = Hotel("H01", "东城旗舰青年驿站", "东城区", "东四十条 1 号", "010-6001")
    h1.beds = make_beds("H01", [("201", 2), ("202", 1)])  # 共 3 床
    h2 = Hotel("H02", "西城枢纽青年驿站", "西城区", "西直门 2 号", "010-6002")
    h2.beds = make_beds("H02", [("301", 1)])              # 仅 1 床（抢最后床位）
    h3 = Hotel("H03", "南城创业青年驿站", "丰台区", "丰台南路 3 号", "010-6003")
    h3.beds = make_beds("H03", [("401", 2)])
    directory.add(h1)
    directory.add(h2)
    directory.add(h3)
    return directory


def build_world(now_fn=None, rate_v1=120.0):
    policies = build_policies()
    directory = build_directory()
    rates = RateCalendar(rate_v1)
    system = LodgingSystem(policies, directory, rates, now_fn or (lambda: NOW))

    duty = system.register_user(Actor("u_duty", "duty_manager", "值班长·马倩"))
    verifier = system.register_user(
        Actor("u_verifier", "verifier", "运营核验员·李核", ["H01", "H02", "H03"]))
    front1 = system.register_user(Actor("u_front1", "hotel_front", "东城前台", ["H01"]))
    front2 = system.register_user(Actor("u_front2", "hotel_front", "西城前台", ["H02"]))
    officer = system.register_user(Actor("u_officer", "service_officer", "团干部·陈服务"))
    finance = system.register_user(Actor("u_finance", "finance", "财政复核员·钱算"))

    return {
        "system": system, "policies": policies, "directory": directory, "rates": rates,
        "duty": duty, "verifier": verifier,
        "front1": front1, "front2": front2, "officer": officer, "finance": finance,
    }


def applicant(system, uid="u_zhang", name="张三", **overrides):
    actor = Actor(uid, "applicant", name)
    system.register_user(actor)
    material = {
        "applicant_id": uid,
        "name": name,
        "phone": "13800000001",
        "id_card_masked": "1301**********0011",
        "household_type": "外地",
        "household_region": "河北省石家庄市",
        "graduation_date": "2025-06-30",
        "degree": "本科",
        "purpose": "求职",
        "visit_intentions": ["某互联网企业", "某智能制造企业"],
        "arrival_note": "高铁 G88 次 14:00 到",
        "target_positions": ["数据分析师"],
        "expected_salary": "8000-10000",
        "resume_summary": "曾在两家企业实习……",
        "portfolio_url": "https://example.test/portfolio",
        "work_history": "2024 实习于 A 公司",
        "reference_contacts": "王经理 139****",
    }
    material.update(overrides)
    return actor, material


def event(event_id, source, etype, hotel, bed, occurred, **extra):
    payload = {
        "event_id": event_id, "source": source, "event_type": etype,
        "hotel_code": hotel, "bed_key": bed, "occurred_at": occurred,
    }
    payload.update(extra)
    return payload


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        world = build_world(self.clock)
        for k, v in world.items():
            setattr(self, k, v)

    def apply(self, material_overrides=None, uid="u_zhang", name="张三"):
        """注册申请人并提交申请，返回 (actor, app, material)。"""
        actor, material = applicant(self.system, uid, name,
                                    **(material_overrides or {}))
        app = self.system.submit_application(actor, material)
        return actor, app, material

    def book(self, actor, app, hotel="H01", start="2026-09-22", end="2026-09-30",
             bed_key=None):
        return self.system.confirm_booking(
            self.__dict__.get(actor) if isinstance(actor, str) else actor,
            app, hotel, start, end, bed_key)

    def check_in(self, front, alloc, day="2026-09-22", hour="14:00", eid="evt_ci"):
        return self.system.ingest_event(front, event(
            eid, "front_desk", "check_in", alloc.hotel_code, alloc.bed_key,
            f"{day}T{hour}:00+08:00"))["result"]
