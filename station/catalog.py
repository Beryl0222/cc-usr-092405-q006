"""酒店站点、床位与房价日历。

"今晚这张床归谁"的唯一答案来自系统层的按夜占用索引 (酒店, 床位, 夜)；
床位实体本身只有 free / blocked（停售维修）两种运营状态，不再保存
held/occupied 等跨夜状态，避免多夜占房在跨日时互相覆盖。
房价按日历维护，但确认时会把每晚价格连同政策补贴单价一并固化到占房记录，
之后房价调整不影响已确认的清算依据。
"""

from dataclasses import dataclass, field

from .errors import NotFoundError, ValidationError
from .timeutil import daterange, iso, parse_date

# 床位运营状态
BED_FREE = "free"       # 正常（是否被订由按夜索引回答）
BED_BLOCKED = "blocked"  # 维修/停售


@dataclass
class Bed:
    hotel_code: str
    room_code: str
    bed_no: str
    bed_type: str = "标准床位"
    status: str = BED_FREE

    @property
    def key(self):
        return f"{self.room_code}-{self.bed_no}"


@dataclass
class Hotel:
    code: str
    name: str
    district: str
    address: str
    front_phone: str
    beds: list = field(default_factory=list)
    online: bool = True  # 站点是否联网（离线仍可补传事件）

    def bed(self, key):
        for b in self.beds:
            if b.key == key:
                return b
        raise NotFoundError("床位不存在", hotel=self.code, bed=key)

    def bed_count(self, status=None):
        return sum(1 for b in self.beds if status is None or b.status == status)


class RateCalendar:
    """每店每晚房价；未显式设置的日期回退到默认价。"""

    def __init__(self, default_rate):
        self.default_rate = float(default_rate)
        self._rates = {}  # (hotel_code, date) -> price

    def set_rate(self, hotel_code, day, price):
        day = parse_date(day)
        self._rates[(hotel_code, day)] = float(price)

    def set_range(self, hotel_code, start, end, price):
        for d in daterange(parse_date(start), parse_date(end)):
            self.set_rate(hotel_code, d, price)

    def rate_on(self, hotel_code, day):
        return self._rates.get((hotel_code, parse_date(day)), self.default_rate)

    def snapshot(self, hotel_code, start, end):
        """固化一段时间的每晚房价。"""
        return {iso(d): self.rate_on(hotel_code, d) for d in daterange(parse_date(start), parse_date(end))}


class HotelDirectory:
    def __init__(self):
        self._hotels = {}

    def add(self, hotel: Hotel):
        if hotel.code in self._hotels:
            raise ValidationError("酒店编号重复", code=hotel.code)
        self._hotels[hotel.code] = hotel
        return hotel

    def get(self, code):
        if code not in self._hotels:
            raise NotFoundError("酒店站点不存在", code=code)
        return self._hotels[code]

    def list(self):
        return [self._hotels[k] for k in sorted(self._hotels)]

    def bed_status_matrix(self, code, start, end):
        """供房态展示：夜 -> 该夜每床的占用来源（由 ledger 回填 allocations）。"""
        raise NotImplementedError  # 由 System 组合 ledger 输出


def make_beds(hotel_code, rooms_spec):
    """rooms_spec: [(房号, 床数), ...]，生成 Bed 列表。"""
    beds = []
    for room_code, count in rooms_spec:
        for n in range(1, count + 1):
            beds.append(Bed(hotel_code, room_code, f"{n:02d}"))
    return beds
