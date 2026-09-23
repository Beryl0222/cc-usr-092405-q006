"""种子数据：28 处合作酒店、两版年度政策、各岗位用户。

种子只负责"世界长什么样"，业务记录一律由正式接口产生。
"""

from datetime import date

from .catalog import Hotel, HotelDirectory, RateCalendar, make_beds
from .models import Actor
from .policy import EligibilityCriteria, PolicyRegistry, PolicyVersion
from .system import LodgingSystem

HOTEL_SPECS = [
    # (编号, 名称, 区, 地址, 房型规格[(房号, 床数)])
    ("H01", "东城旗舰青年驿站", "东城区", "东四十条 1 号", [("201", 2), ("202", 2)]),
    ("H02", "西城枢纽青年驿站", "西城区", "西直门南大街 2 号", [("301", 2), ("302", 1)]),
    ("H03", "朝阳 CBD 青年驿站", "朝阳区", "建国路 3 号", [("401", 2), ("402", 2)]),
    ("H04", "海淀学院路青年驿站", "海淀区", "学院路 4 号", [("501", 2), ("502", 2)]),
    ("H05", "丰台创业港青年驿站", "丰台区", "丰台南路 5 号", [("601", 2), ("602", 1)]),
    ("H06", "石景山首钢青年驿站", "石景山区", "石景山路 6 号", [("701", 2)]),
    ("H07", "通州副中心青年驿站", "通州区", "新华大街 7 号", [("801", 2), ("802", 2)]),
    ("H08", "大兴机场线青年驿站", "大兴区", "兴华大街 8 号", [("901", 2), ("902", 1)]),
    ("H09", "昌平未来城青年驿站", "昌平区", "未来科学城 9 号", [("1001", 2)]),
    ("H10", "顺义临空青年驿站", "顺义区", "顺平路 10 号", [("1101", 2), ("1102", 1)]),
    ("H11", "房山长阳青年驿站", "房山区", "长阳路 11 号", [("1201", 2)]),
    ("H12", "门头沟永定青年驿站", "门头沟区", "永定路 12 号", [("1301", 2)]),
    ("H13", "平谷金海湖青年驿站", "平谷区", "金海湖路 13 号", [("1401", 2)]),
    ("H14", "怀柔科学城青年驿站", "怀柔区", "雁栖湖路 14 号", [("1501", 2), ("1502", 1)]),
    ("H15", "密云生态青年驿站", "密云区", "密云水库路 15 号", [("1601", 2)]),
    ("H16", "延庆冬奥青年驿站", "延庆区", "妫水街 16 号", [("1701", 2)]),
    ("H17", "东城王府井青年驿站", "东城区", "王府井大街 17 号", [("1801", 2), ("1802", 2)]),
    ("H18", "西城金融街青年驿站", "西城区", "金融大街 18 号", [("1901", 2)]),
    ("H19", "朝阳望京青年驿站", "朝阳区", "望京街 19 号", [("2001", 2), ("2002", 2)]),
    ("H20", "海淀中关村青年驿站", "海淀区", "中关村大街 20 号", [("2101", 2), ("2102", 1)]),
    ("H21", "丰台丽泽青年驿站", "丰台区", "丽泽路 21 号", [("2201", 2)]),
    ("H22", "朝阳奥体青年驿站", "朝阳区", "奥体中心路 22 号", [("2301", 2), ("2302", 1)]),
    ("H23", "海淀上地青年驿站", "海淀区", "上地信息路 23 号", [("2401", 2)]),
    ("H24", "大兴亦庄青年驿站", "大兴区", "亦庄科创路 24 号", [("2501", 2), ("2502", 2)]),
    ("H25", "通州运河青年驿站", "通州区", "运河大街 25 号", [("2601", 2)]),
    ("H26", "昌平回龙观青年驿站", "昌平区", "回龙观大街 26 号", [("2701", 2), ("2702", 1)]),
    ("H27", "顺义后沙峪青年驿站", "顺义区", "后沙峪路 27 号", [("2801", 2)]),
    ("H28", "房山良乡青年驿站", "房山区", "良乡大街 28 号", [("2901", 2), ("2902", 1)]),
]

# 默认房价（元/间夜），个别店可单独调价
DEFAULT_RATE = 120.0


def build_seed_system(now_fn=None):
    policies = PolicyRegistry()
    policies.add(PolicyVersion(
        "P2025", "2025 年度青年驿站住宿办法", date(2025, 1, 1),
        EligibilityCriteria(
            household_types=("外地",),
            max_years_since_graduation=2,
            degrees=("大专", "本科", "硕士", "博士"),
            purposes=("求职", "面试", "参加招聘活动", "入职报到")),
        effective_to=date(2025, 12, 31),
        max_free_nights=30, subsidy_per_night=80.0,
        check_in_grace_hours=18, free_cancel_hours=24,
        emergency_extension_nights=3))
    policies.add(PolicyVersion(
        "P2026", "2026 年度青年驿站住宿办法", date(2026, 1, 1),
        EligibilityCriteria(
            household_types=("外地",),
            max_years_since_graduation=3,
            degrees=("本科", "硕士", "博士"),
            purposes=("求职", "面试", "参加招聘活动", "入职报到", "创业考察")),
        max_free_nights=30, subsidy_per_night=100.0,
        check_in_grace_hours=18, free_cancel_hours=24,
        emergency_extension_nights=3))

    directory = HotelDirectory()
    for code, name, district, address, rooms in HOTEL_SPECS:
        hotel = Hotel(code, name, district, address, f"010-6{code[1:]}00")
        hotel.beds = make_beds(code, rooms)
        directory.add(hotel)

    rates = RateCalendar(DEFAULT_RATE)
    # 旺季调价示例：国庆期间 H03/H20 上调
    rates.set_range("H03", "2026-10-01", "2026-10-07", 180.0)
    rates.set_range("H20", "2026-10-01", "2026-10-07", 160.0)

    system = LodgingSystem(policies, directory, rates, now_fn)

    system.register_user(Actor("u_duty", "duty_manager", "值班长·马倩"))
    system.register_user(Actor("u_verifier", "verifier", "运营核验员·李核",
                               [s[0] for s in HOTEL_SPECS]))
    system.register_user(Actor("u_officer", "service_officer", "团干部·陈服务"))
    system.register_user(Actor("u_finance", "finance", "财政复核员·钱算"))
    for code, name, *_ in HOTEL_SPECS:
        system.register_user(Actor(f"u_front_{code.lower()}", "hotel_front",
                                   f"{name}前台", [code]))
    return system


def check_seed(system):
    """--check 用：验证种子完整性。"""
    hotels = system.directory.list()
    assert len(hotels) == 28, f"酒店数量 {len(hotels)} != 28"
    assert system.duty_manager_id, "缺少值班长"
    beds = sum(h.bed_count() for h in hotels)
    assert beds > 0, "没有可用床位"
    versions = system.policies.list()
    assert len(versions) == 2, "政策版本数量异常"
    return {"hotels": len(hotels), "beds": beds,
            "policies": [p.code for p in versions]}
