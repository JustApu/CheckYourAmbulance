"""Pure station-calendar recurrence and UTC window calculations (TIME-2/3)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

RECURRENCES = ("DAILY", "WEEKDAY", "WEEKEND", "INTERVAL")


def utc_instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Rule:
    zone: str
    wall_time: time
    effective_at: datetime
    recurrence: str = "DAILY"
    early_seconds: int = 3600
    grace_seconds: int = 3600
    interval_days: int | None = None
    anchor_date: date | None = None

    def __post_init__(self):
        ZoneInfo(self.zone)
        if not isinstance(self.wall_time, time) or self.wall_time.tzinfo is not None:
            raise ValueError("wall_time must be a naive time")
        object.__setattr__(self, "effective_at", utc_instant(self.effective_at))
        if self.recurrence not in RECURRENCES:
            raise ValueError("Unknown recurrence")
        for seconds in (self.early_seconds, self.grace_seconds):
            if type(seconds) is not int or not 0 <= seconds <= 2147483647:
                raise ValueError("Window seconds must be nonnegative signed 32-bit integers")
        if self.recurrence == "INTERVAL":
            if type(self.interval_days) is not int or not 1 <= self.interval_days <= 2147483647:
                raise ValueError("INTERVAL requires positive interval_days")
            if type(self.anchor_date) is not date:
                raise ValueError("INTERVAL requires anchor_date")
        elif self.interval_days is not None or self.anchor_date is not None:
            raise ValueError("Interval parameters are only valid for INTERVAL")


def resolve_local(day, wall_time, zone):
    """Resolve first fold occurrence or the exact end of a gap, returning UTC.

    PEP 495 permits constructing imaginary times. Round trips detect them;
    their two offsets bracket the forward transition. Binary search locates
    its first valid microsecond, rather than shifting 02:30 to 03:30.
    """
    local = datetime.combine(day, wall_time)
    tz = ZoneInfo(zone)
    candidates = sorted(local.replace(tzinfo=tz, fold=f).astimezone(UTC) for f in (0, 1))
    valid = [c for c in candidates if c.astimezone(tz).replace(tzinfo=None) == local]
    if valid:
        return valid[0]
    low, high = candidates
    while high - low > timedelta(microseconds=1):
        middle = low + (high - low) // 2
        if middle.astimezone(tz).replace(tzinfo=None) >= local:
            high = middle
        else:
            low = middle
    return high


@dataclass(frozen=True)
class Window:
    due_at: datetime
    early_at: datetime
    grace_at: datetime


def window(due_at, early_seconds=3600, grace_seconds=3600):
    due = utc_instant(due_at)
    for seconds in (early_seconds, grace_seconds):
        if type(seconds) is not int or not 0 <= seconds <= 2147483647:
            raise ValueError("Invalid window seconds")
    return Window(
        due, due - timedelta(seconds=early_seconds), due + timedelta(seconds=grace_seconds)
    )


def due_instants(rule, start, end):
    """Yield ordered, distinct UTC due instants in inclusive [start, end].

    INTERVAL means every N station-calendar days from anchor_date (not an
    elapsed duration). Streaming bounds memory; the finite date range bounds
    work. Two days of padding cover even a skipped civil date (Pacific/Apia).
    """
    start, end = utc_instant(start), utc_instant(end)
    if start > end:
        raise ValueError("start must not exceed end")
    tz = ZoneInfo(rule.zone)
    first = date.fromordinal(max(1, start.astimezone(tz).date().toordinal() - 2))
    last = min(date.max.toordinal(), end.astimezone(tz).date().toordinal() + 2)
    previous = None
    for ordinal in range(first.toordinal(), last + 1):
        day = date.fromordinal(ordinal)
        matches = (
            rule.recurrence == "DAILY"
            or (rule.recurrence == "WEEKDAY" and day.weekday() < 5)
            or (rule.recurrence == "WEEKEND" and day.weekday() >= 5)
            or (
                rule.recurrence == "INTERVAL"
                and day >= rule.anchor_date
                and (day - rule.anchor_date).days % rule.interval_days == 0
            )
        )
        if matches:
            due = resolve_local(day, rule.wall_time, rule.zone)
            if max(start, utc_instant(rule.effective_at)) <= due <= end and due != previous:
                previous = due
                yield due


def windows(rule, start, end):
    for due in due_instants(rule, start, end):
        yield window(due, rule.early_seconds, rule.grace_seconds)
