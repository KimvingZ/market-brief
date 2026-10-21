"""Time helpers: market calendars, bar close times and session boundaries.

All datetimes inside the package are timezone-aware UTC. Daily bars are keyed
by their trading date (a datetime.date). When a daily bar is complete depends
on the asset's calendar:

  crypto     the bar for date D closes at D+1 00:00 UTC
  us_equity  the bar for date D closes at D 16:00 New York
  cme        the bar for date D closes at D 17:00 New York (gold, oil futures)

Holidays are not modelled. A missing holiday bar just means the next
available bar is used when grading.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
PHNOM_PENH = ZoneInfo("Asia/Phnom_Penh")

CALENDARS = ("crypto", "us_equity", "cme")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_ts(value) -> datetime:
    """Parse an ISO string (with Z or offset) or a datetime into aware UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return parse_ts(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def local_label(dt: datetime, tz: ZoneInfo = PHNOM_PENH) -> str:
    """'Wed 21 Oct 07:35' in the display time zone."""
    return parse_ts(dt).astimezone(tz).strftime("%a %d %b %H:%M")


def is_trading_day(calendar: str, d: date) -> bool:
    if calendar == "crypto":
        return True
    return d.weekday() < 5


def bar_close_time(calendar: str, d: date) -> datetime:
    """When the daily bar for trading date d is complete, in UTC."""
    if calendar == "crypto":
        return datetime.combine(d + timedelta(days=1), time(0, 0), UTC)
    if calendar == "us_equity":
        return datetime.combine(d, time(16, 0), NEW_YORK).astimezone(UTC)
    if calendar == "cme":
        return datetime.combine(d, time(17, 0), NEW_YORK).astimezone(UTC)
    raise ValueError(f"unknown calendar {calendar!r}")


def next_trading_dates(calendar: str, after: date, n: int) -> list[date]:
    """The next n trading dates strictly after `after` (weekdays for non-crypto)."""
    out: list[date] = []
    d = after
    while len(out) < n:
        d = d + timedelta(days=1)
        if is_trading_day(calendar, d):
            out.append(d)
    return out


def nth_trading_date(calendar: str, after: date, n: int) -> date:
    return next_trading_dates(calendar, after, n)[-1]


def first_bar_closing_after(calendar: str, t: datetime) -> date:
    """The first trading date whose bar closes strictly after time t."""
    t = parse_ts(t)
    d = t.date() - timedelta(days=2)
    for _ in range(14):
        if is_trading_day(calendar, d) and bar_close_time(calendar, d) > t:
            return d
        d = d + timedelta(days=1)
    raise RuntimeError("no trading date found")  # pragma: no cover


def last_bar_closed_by(calendar: str, t: datetime) -> date:
    """The latest trading date whose bar has closed at or before time t."""
    t = parse_ts(t)
    d = t.date() + timedelta(days=1)
    for _ in range(14):
        if is_trading_day(calendar, d) and bar_close_time(calendar, d) <= t:
            return d
        d = d - timedelta(days=1)
    raise RuntimeError("no trading date found")  # pragma: no cover


def trading_days_between(calendar: str, a: date, b: date) -> int:
    """Number of trading dates in (a, b]."""
    if b <= a:
        return 0
    count = 0
    d = a
    while d < b:
        d = d + timedelta(days=1)
        if is_trading_day(calendar, d):
            count += 1
    return count


def session_bounds(d: date) -> dict[str, tuple[datetime, datetime]]:
    """Session windows for UTC date d, following UK and US daylight saving.

    asia    00:00 UTC        -> 08:00 London   (London open)
    london  08:00 London     -> 09:00 New York (just after US data at 08:30)
    ny      09:00 New York   -> 16:00 New York (US close)
    """
    asia_start = datetime.combine(d, time(0, 0), UTC)
    london_open = datetime.combine(d, time(8, 0), LONDON).astimezone(UTC)
    ny_nine = datetime.combine(d, time(9, 0), NEW_YORK).astimezone(UTC)
    ny_close = datetime.combine(d, time(16, 0), NEW_YORK).astimezone(UTC)
    return {
        "asia": (asia_start, london_open),
        "london": (london_open, ny_nine),
        "ny": (ny_nine, ny_close),
    }


def ny_time(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), NEW_YORK).astimezone(UTC)
