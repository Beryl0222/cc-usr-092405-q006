"""时间与日期工具：全域只使用 Asia/Shanghai 的日历日，不依赖第三方库。"""

from datetime import date, datetime, timezone, timedelta, tzinfo

CST = timezone(timedelta(hours=8))


def today(now=None):
    return (now or datetime.now(tz=CST)).date()


def now_cst():
    return datetime.now(tz=CST)


def parse_date(value):
    """接受 ISO 日期字符串或 date；空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(CST).date()
    text = str(value).strip()
    if not text:
        return None
    # 允许传入完整时间戳，截取日期部分
    if "T" in text or " " in text:
        text = text.replace(" ", "T").split("T")[0]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析日期: {value!r}") from exc


def parse_dt(value):
    """解析带时区的时间；无时区按 CST 处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def daterange(start, end):
    """闭区间逐日生成。end < start 时为空。"""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def iso(d):
    return None if d is None else d.isoformat()
