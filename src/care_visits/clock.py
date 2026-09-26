"""时钟抽象。

业务计时（升级时限、跨午夜、漏访判定）全部经过 Clock，生产环境用 SystemClock，
测试用 FakeClock 精确推进，避免对真实时间的依赖。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """从固定起点开始、只能向前推进的测试时钟。"""

    def __init__(self, start: Optional[datetime] = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
        self._now += timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._now = value.astimezone(timezone.utc)
