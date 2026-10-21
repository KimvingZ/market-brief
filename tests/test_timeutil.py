from datetime import date, datetime

from brief.timeutil import (
    UTC,
    bar_close_time,
    first_bar_closing_after,
    last_bar_closed_by,
    nth_trading_date,
    session_bounds,
)


def test_bar_close_times():
    d = date(2026, 10, 21)
    assert bar_close_time("crypto", d) == datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
    assert bar_close_time("us_equity", d) == datetime(2026, 10, 21, 20, 0, tzinfo=UTC)  # EDT
    assert bar_close_time("cme", d) == datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
    assert bar_close_time("us_equity", date(2026, 11, 2)) == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)  # EST


def test_sessions_follow_daylight_saving():
    b = session_bounds(date(2026, 10, 21))  # UK summer time, US summer time
    assert b["london"][0] == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    assert b["ny"][0] == datetime(2026, 10, 21, 13, 0, tzinfo=UTC)
    b = session_bounds(date(2026, 10, 27))  # UK winter, US still summer
    assert b["london"][0] == datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    assert b["ny"][0] == datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    b = session_bounds(date(2026, 11, 3))  # both winter
    assert b["ny"] == (datetime(2026, 11, 3, 14, 0, tzinfo=UTC), datetime(2026, 11, 3, 21, 0, tzinfo=UTC))


def test_trading_dates_skip_weekends():
    fri = date(2026, 10, 23)
    assert nth_trading_date("us_equity", fri, 1) == date(2026, 10, 26)
    assert nth_trading_date("crypto", fri, 1) == date(2026, 10, 24)
    assert nth_trading_date("cme", fri, 5) == date(2026, 10, 30)


def test_first_and_last_bar():
    sat = datetime(2026, 10, 24, 0, 35, tzinfo=UTC)
    assert last_bar_closed_by("us_equity", sat) == date(2026, 10, 23)
    assert first_bar_closing_after("us_equity", sat) == date(2026, 10, 26)
    assert last_bar_closed_by("crypto", sat) == date(2026, 10, 23)
    mon = datetime(2026, 10, 26, 0, 35, tzinfo=UTC)
    assert first_bar_closing_after("cme", mon) == date(2026, 10, 26)
