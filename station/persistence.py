"""系统状态快照与恢复。

自动化场景会在跨日截止点推进、并发退订后重启服务。快照把全部业务记录
（含候补条目、保留方案、单调序号、已消费确认令牌）固化为 JSON 安全结构；
恢复时按仍占用夜重建 (酒店,床位,夜) 与 (申请人,夜) 唯一索引，因此：

- 晋位顺序不变：排序依据（紧急程度、资格决定时间、申请顺序序号）整体固化；
- 保留期限不变：offer.expires_at 是绝对时刻，重启不会重新计时；
- 幂等不丢：已消费的 request_id 随快照恢复，离线重复确认不会二次扣权益。
"""

import json
from dataclasses import asdict

from .models import *
from .system import OCCUPYING
from .timeutil import parse_date

SNAPSHOT_VERSION = 1


def snapshot_system(system):
    """生成 JSON 安全的系统快照。"""
    with system.lock:
        data = {
            "version": SNAPSHOT_VERSION,
            "duty_manager_id": system.duty_manager_id,
            "waitlist_seq": system._waitlist_seq,
            "consumed_confirm_ids": sorted(system._consumed_confirm_ids),
            "users": [asdict(u) for u in system.users.values()],
            "applications": [asdict(a) for a in system.applications.values()],
            "allocations": [_allocation_to_dict(a) for a in system.allocations.values()],
            "events": [asdict(e) for e in system.events.values()],
            "tickets": [asdict(t) for t in system.tickets.values()],
            "requests": [asdict(r) for r in system.requests.values()],
            "settlements": [asdict(s) for s in system.settlements.values()],
            "waitlist": [asdict(w) for w in system.waitlist.values()],
            "offers": [asdict(o) for o in system.offers.values()],
        }
    return data


def dump_json(system, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot_system(system), fh, ensure_ascii=False, default=str)


def _allocation_to_dict(alloc):
    out = asdict(alloc)
    out["nights"] = {d: asdict(line) for d, line in alloc.nights.items()}
    return out


def restore_system(system, data):
    """把快照恢复进一个（通常是新建的）系统，并重建派生索引。"""
    if data.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"不支持的快照版本: {data.get('version')}")
    with system.lock:
        system.users = {u["id"]: Actor(**u) for u in data["users"]}
        system.duty_manager_id = data.get("duty_manager_id")

        system.applications = {}
        for a in data["applications"]:
            app = Application(**a)
            system.applications[app.id] = app

        system.allocations = {}
        for raw in data["allocations"]:
            nights = {d: NightLine(**line) for d, line in raw.pop("nights").items()}
            alloc = Allocation(**raw)
            alloc.nights = nights
            system.allocations[alloc.id] = alloc

        system.events = {e["event_id"]: EventRecord(**e) for e in data["events"]}
        system.tickets = {t["id"]: ReviewTicket(**t) for t in data["tickets"]}
        system.requests = {r["id"]: ServiceRequest(**r) for r in data["requests"]}
        system.settlements = {s["id"]: Settlement(**s) for s in data["settlements"]}

        system.waitlist = {w["id"]: WaitlistEntry(**w) for w in data["waitlist"]}
        system.offers = {o["id"]: WaitlistOffer(**o) for o in data["offers"]}
        system._waitlist_seq = int(data.get("waitlist_seq", 0))
        system._consumed_confirm_ids = set(data.get("consumed_confirm_ids", ()))

        _rebuild_indices(system)
    return system


def _rebuild_indices(system):
    """从占房记录与事件重建派生的唯一索引与去重指纹。"""
    system._bed_night = {}
    system._person_night = {}
    for alloc in system.allocations.values():
        for day, line in alloc.nights.items():
            # 只有仍占用（含暂时保留的 held、争议冻结）才占位；released 不占
            if line.state in OCCUPYING:
                system._bed_night[(alloc.hotel_code, alloc.bed_key, day)] = alloc.id
                system._person_night[(alloc.applicant_id, day)] = alloc.id

    system._fingerprints = {}
    for event in system.events.values():
        # 运行时只有非"语义重复"的事件登记指纹；duplicate 事件不登记
        if event.status == "duplicate":
            continue
        fp = _event_fingerprint(event)
        if fp and fp not in system._fingerprints:
            system._fingerprints[fp] = event.event_id


def _event_fingerprint(event):
    from datetime import timedelta
    from .timeutil import parse_dt
    who = event.bed_key or event.applicant_id
    if not who:
        return None
    occurred = parse_dt(event.occurred_at)
    bucket = occurred.replace(second=0, microsecond=0,
                              minute=occurred.minute - occurred.minute % 5)
    return (event.hotel_code, who, event.event_type, bucket.isoformat())
