"""时间、时区与工作日历。

约定（见 docs/contracts.md）：
- 所有时间以带偏移量的 ISO-8601 字符串存储，内部统一为时区感知 datetime。
- 工作日按 calendar 记录：周一至周五扣除 holidays，并加上 make_up_days。
- 送达后的第一个工作日为第一个计数日，截止到当地该日 18:00。
- 有效性检查两个工作日；继续维持确认五个工作日。
- 未送达不开始倒计时。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CUTOFF = time(18, 0, 0)


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO-8601 时间，要求带时区信息。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区偏移量: {value!r}")
    return dt.astimezone(ZoneInfo("UTC"))


def format_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("拒绝输出无时区时间")
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def to_local(dt: datetime, tzname: str) -> datetime:
    return dt.astimezone(ZoneInfo(tzname))


class Clock:
    """可注入时钟；测试通过请求头 X-Current-Time 固定“现在”。"""

    def __init__(self, now: datetime):
        self._now = parse_dt(now)

    @property
    def now(self) -> datetime:
        return self._now

    def at(self, now: datetime | str) -> "Clock":
        return Clock(parse_dt(now))


@dataclass(frozen=True)
class WorkingCalendar:
    code: str
    timezone: str
    holidays: frozenset[date] = field(default_factory=frozenset)
    make_up_days: frozenset[date] = field(default_factory=frozenset)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_workday(self, day: date) -> bool:
        if day in self.make_up_days:
            return True
        if day in self.holidays:
            return False
        return day.weekday() < 5

    def first_workday_on_or_after(self, day: date) -> date:
        cur = day
        while not self.is_workday(cur):
            cur += timedelta(days=1)
        return cur

    def add_workdays(self, start_day: date, n: int) -> date:
        """从 start_day 之后的第一个工作日起计，第 n 个工作日（n>=1）。"""
        if n < 1:
            raise ValueError("工作日数必须为正整数")
        cur = self.first_workday_on_or_after(start_day + timedelta(days=1))
        count = 1
        while count < n:
            cur += timedelta(days=1)
            cur = self.first_workday_on_or_after(cur)
            count += 1
        return cur

    def local_date(self, moment: datetime) -> date:
        return to_local(parse_dt(moment), self.timezone).date()

    def deadline(self, served_at: datetime, workdays: int) -> datetime:
        """送达时刻后的第 workdays 个工作日当地 18:00（转换为 UTC 感知时间）。"""
        served_local = to_local(parse_dt(served_at), self.timezone)
        due_day = self.add_workdays(served_local.date(), workdays)
        due_local = datetime.combine(due_day, CUTOFF, tzinfo=self.tz)
        return due_local.astimezone(ZoneInfo("UTC"))

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "timezone": self.timezone,
            "holidays": sorted(d.isoformat() for d in self.holidays),
            "make_up_days": sorted(d.isoformat() for d in self.make_up_days),
        }
