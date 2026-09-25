"""领域实体与序列化。全部使用日历日字符串持久化，便于接口直接输出。"""

from dataclasses import dataclass, field, asdict

# 申请状态
APP_SUBMITTED = "submitted"      # 已提交待核验
APP_ELIGIBLE = "eligible"        # 资格核验通过（快照已固化）
APP_DENIED = "denied"            # 资格不满足且不可申诉
APP_IN_REVIEW = "in_review"      # 临界/争议，人工复核中
APP_WITHDRAWN = "withdrawn"      # 申请人撤回

# 候补条目状态
WL_WAITING = "waiting"          # 在队，等待匹配
WL_OFFERED = "offered"          # 已匹配，暂时性占房待确认
WL_CONFIRMED = "confirmed"      # 已确认，转为正式占房
WL_EXPIRED = "expired"          # 超过最晚确认时间未确认（或保留期超时）
WL_REJECTED = "rejected"        # 申请人拒绝方案
WL_CANCELLED = "cancelled"      # 申请人主动撤下
WL_BLOCKED = "blocked"          # 登记时/匹配时发现资格冲突，转人工或排除出队

# 候补活跃状态（仍占队位或暂时占房）
WL_ACTIVE = {WL_WAITING, WL_OFFERED}

# 保留方案默认有效时长（小时）
DEFAULT_OFFER_HOLD_MINUTES = 120

# 占房夜状态
NIGHT_HELD = "held"              # 已确认占房，尚未入住
NIGHT_STAYED = "stayed"          # 实际在店
NIGHT_AWAY = "away"              # 临时离店（房间保留）
NIGHT_RELEASED = "released"      # 已释放（撤回/取消）
NIGHT_NO_SHOW = "no_show"        # 爽约（人工裁决）
NIGHT_OVERSTAY = "overstay"      # 超期占用，待裁决
NIGHT_BLOCKED = "blocked"        # 争议冻结，暂不清算

# 消耗权益的夜状态
ENTITLEMENT_CONSUMING = {NIGHT_HELD, NIGHT_STAYED, NIGHT_AWAY, NIGHT_OVERSTAY}
# 可计入财政清算的夜状态：仅实际在店（临时离店不在店，不清算，归还后恢复）
BILLABLE = {NIGHT_STAYED}

# 人工复核工单类型
REVIEW_ELIGIBILITY_APPEAL = "eligibility_appeal"
REVIEW_NO_SHOW = "no_show"
REVIEW_OVERSTAY = "overstay"
REVIEW_EMERGENCY_EXTENSION = "emergency_extension"
REVIEW_EVENT_CONFLICT = "event_conflict"
REVIEW_UNKNOWN_EVENT = "unknown_event"
REVIEW_UNCLOSED_LEAVE = "unclosed_leave"

# 工单状态
TICKET_OPEN = "open"
TICKET_APPROVED = "approved"
TICKET_REJECTED = "rejected"

# 事件来源
SRC_DOOR_LOCK = "door_lock"
SRC_FRONT_DESK = "front_desk"
SRC_SYSTEM = "system"


@dataclass
class Actor:
    id: str
    role: str
    name: str
    # 数据范围：酒店编号列表；None 表示不受限
    hotels: object = None

    def can_access_hotel(self, hotel_code):
        return self.hotels is None or hotel_code in self.hotels


@dataclass
class Application:
    id: str
    applicant_id: str
    material: dict
    submitted_on: str
    policy_code: str
    status: str = APP_SUBMITTED
    eligibility: object = None  # EligibilityDecision.to_dict()
    tickets: list = field(default_factory=list)
    withdrawn_at: object = None


@dataclass
class NightLine:
    """一个占房夜的全部清算依据。"""

    date: str
    state: str = NIGHT_HELD
    billable: bool = False
    compliant: bool = True
    rate_locked: float = 0.0       # 确认时固化的酒店房价
    subsidy_locked: float = 0.0    # 确认时固化的政策补贴单价
    policy_code: str = ""
    events: list = field(default_factory=list)  # 佐证事件 id

    def to_dict(self):
        return asdict(self)


@dataclass
class Allocation:
    """一处占房（可跨多夜，可因调剂换店——换店采用原单关闭+新单承接）。"""

    id: str
    app_id: str
    applicant_id: str
    hotel_code: str
    bed_key: str
    start: str
    end: str
    nights: dict = field(default_factory=dict)  # iso date -> NightLine
    planned_end: str = ""        # 申请确认时的原定结束日（调剂后保持不变）
    policy_code: str = ""
    chain_id: str = ""           # 调剂链：同一串占房共享 chain_id
    created_from: str = "direct"  # direct / transfer / waitlist_offer
    created_at: object = None
    check_in_at: object = None
    check_out_at: object = None
    settlement_id: object = None
    # 候补暂时性占房：provisional=True 时床位已在唯一索引内保留但尚未确认，
    # 任何终局（确认/超时/拒绝/撤下）都会原子翻转或释放。
    provisional: bool = False
    waitlist_entry_id: object = None
    offer_id: object = None
    offered_at: object = None
    offer_expires_at: object = None
    confirmed_at: object = None

    def date_list(self):
        return sorted(self.nights)

    def consuming_count(self):
        # 暂时性占房同样占用权益额度，防止候补期间重复占用补贴天数
        return sum(1 for n in self.nights.values() if n.state in ENTITLEMENT_CONSUMING)


@dataclass
class EventRecord:
    event_id: str
    source: str
    hotel_code: str
    bed_key: str
    event_type: str            # check_in / door_open / temp_leave / return / check_out
    occurred_at: str           # ISO 时间（事件实际发生）
    recorded_at: str           # ISO 时间（系统接收/补传）
    allocation_id: object = None
    applicant_id: object = None
    payload: dict = field(default_factory=dict)
    status: str = "applied"    # applied / duplicate / ignored / pending
    duplicate_of: object = None
    ticket_id: object = None
    offline: bool = False


@dataclass
class ReviewTicket:
    id: str
    type: str
    hotel_code: str
    applicant_id: str
    allocation_id: object
    created_at: str
    owner_id: str              # 人工例外责任人（值班长）
    owner_name: str
    status: str = TICKET_OPEN
    summary: str = ""
    evidence: dict = field(default_factory=dict)
    decided_at: object = None
    decision: object = None    # approved / rejected
    decision_note: str = ""
    decider_id: object = None


@dataclass
class ServiceRequest:
    """服务诉求（企业参访、咨询、投诉等）。"""

    id: str
    app_id: str
    applicant_id: str
    kind: str                  # enterprise_visit / consultation / complaint
    content: str
    created_at: str
    status: str = "open"       # open / handling / closed
    handler_id: object = None
    handler_name: object = None
    messages: list = field(default_factory=list)


@dataclass
class Settlement:
    id: str
    allocation_id: str
    applicant_id: str
    hotel_code: str
    policy_code: str
    created_at: str
    finance_id: str
    lines: list = field(default_factory=list)
    total_subsidy: float = 0.0
    total_nights: int = 0


@dataclass
class WaitlistEntry:
    """候补登记：一段连续日期 + 可接受酒店集合 + 最晚确认时间。

    rank_basis 是登记瞬间固化的排序解释（资格决定时间、紧急程度、申请顺序），
    之后政策/时间推进都不改变既有队列的先后依据，保证跨日与重启后晋位顺序不变。
    """

    id: str
    app_id: str
    applicant_id: str
    start: str
    end: str
    hotels: list                       # 可接受酒店编号（有序，按偏好）
    registered_at: str
    latest_confirm_at: str             # 申请人给出的最晚确认时刻
    rank_basis: dict                   # 固化的可解释排序依据
    status: str = WL_WAITING
    bed_key: object = None             # 登记时的偏好床（可空）
    note: str = ""
    # 状态推进留痕
    offered_at: object = None
    offer_expires_at: object = None
    offer_id: object = None
    allocation_id: object = None
    decided_at: object = None
    decision: object = None            # confirmed / expired / rejected / cancelled
    history: list = field(default_factory=list)

    def active(self):
        return self.status in WL_ACTIVE


@dataclass
class WaitlistOffer:
    """一次暂时性保留方案。一个条目同一时刻至多一个未决 offer。"""

    id: str
    entry_id: str
    applicant_id: str
    app_id: str
    hotel_code: str
    bed_key: str
    start: str
    end: str
    allocation_id: str
    created_at: str
    expires_at: str                    # 保留期限（绝对时刻，重启后不重新计时）
    request_id: object = None          # 已消费的确认幂等令牌
    status: str = "open"               # open / confirmed / expired / rejected / cancelled
    decided_at: object = None
    decision_note: str = ""
