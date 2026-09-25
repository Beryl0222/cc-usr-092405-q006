"""领域实体与序列化。全部使用日历日字符串持久化，便于接口直接输出。"""

import uuid
from dataclasses import dataclass, field, asdict


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

# 申请状态
APP_SUBMITTED = "submitted"      # 已提交待核验
APP_ELIGIBLE = "eligible"        # 资格核验通过（快照已固化）
APP_DENIED = "denied"            # 资格不满足且不可申诉
APP_IN_REVIEW = "in_review"      # 临界/争议，人工复核中
APP_WITHDRAWN = "withdrawn"      # 申请人撤回

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

# 候补条目状态
WAIT_QUEUED = "queued"          # 排队中，等待房态恢复
WAIT_OFFERED = "offered"        # 已出暂时保留方案，等待申请人确认
WAIT_CONFIRMED = "confirmed"    # 已确认（占房已落地，候补闭合）
WAIT_DECLINED = "declined"      # 申请人主动拒绝保留
WAIT_EXPIRED = "expired"        # 最晚确认时间超时，保留已释放
WAIT_CANCELLED = "cancelled"    # 申请人撤回候补
WAIT_BLOCKED = "blocked"        # 出现资格/冻结/工单/住宿冲突，暂不参与晋位

# 候补终态（不再参与队列推进）
WAIT_FINAL = {WAIT_CONFIRMED, WAIT_DECLINED, WAIT_EXPIRED, WAIT_CANCELLED}
# 紧急程度（值越大越优先；排序时在资格时间之后、申请顺序之前使用）
URGENCY_NORMAL = "normal"
URGENCY_RECRUIT = "recruit"            # 大型招聘活动期间
URGENCY_ARRIVING = "arriving_today"    # 当日抵城、车次/活动已临近
URGENCY_ORDER = {URGENCY_NORMAL: 0, URGENCY_RECRUIT: 1, URGENCY_ARRIVING: 2}


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
    created_from: str = "direct"  # direct / transfer
    created_at: object = None
    check_in_at: object = None
    check_out_at: object = None
    settlement_id: object = None

    def date_list(self):
        return sorted(self.nights)

    def consuming_count(self):
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
    """候补队列条目。

    排序口径（晋位顺序）在登记时固化为 rank_key 并以 reasons 解释：
    资格决定时间（越早越优先）→ 紧急程度 → 申请顺序（全局自增序号）。
    房态恢复后至多生成一个暂时保留方案（offer）；确认/超时/拒绝都在同一把
    锁内原子释放床位索引并推动下一位。
    """

    id: str
    app_id: str
    applicant_id: str
    start: str                       # 连续日期起（含）
    end: str                         # 连续日期止（含）
    preferred_hotels: list           # 可接受酒店编号（按偏好排序）
    register_seq: int                # 全局申请顺序，同条件下先登记先得
    decided_on: str                  # 资格决定时间（资格快照日；申诉通过则取裁决日）
    urgency: str = URGENCY_NORMAL
    rank_key: list = field(default_factory=list)      # 固化的晋位排序键
    rank_reasons: dict = field(default_factory=dict)  # 可解释队列的排序依据
    status: str = WAIT_QUEUED
    # 暂时保留方案（queued 时为 None）
    offer_hotel: object = None
    offer_bed: object = None
    offer_nights: list = field(default_factory=list)
    offered_at: object = None
    confirm_deadline: object = None  # 最晚确认时间（绝对时间，ISO）
    offer_token: object = None       # 保留方案版本号：每次出队生成新令牌
    allocation_id: object = None     # 确认后落地的占房
    # 阻塞原因（blocked 状态）：existing_stay / frozen / open_ticket / quota / policy
    blocked_reason: object = None
    blocked_detail: dict = field(default_factory=dict)
    created_at: object = None
    updated_at: object = None
    history: list = field(default_factory=list)       # 状态流转留痕
    # 最近一次晋位为何没匹配上（解释"为什么还没轮到我"）
    last_skip_reason: object = None
