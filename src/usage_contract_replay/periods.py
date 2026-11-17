"""Billing period window calculation."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .models import Period
from .util import InputError


SUPPORTED_PERIODS = ("month", "quarter", "year")


def _add_months(year: int, month: int, count: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + count
    return index // 12, index % 12 + 1


def _period_bounds(period: str, value: date) -> tuple[date, date]:
    if period == "month":
        start_year, start_month = value.year, value.month
        length = 1
    elif period == "quarter":
        start_year = value.year
        start_month = (value.month - 1) // 3 * 3 + 1
        length = 3
    elif period == "year":
        start_year, start_month = value.year, 1
        length = 12
    else:  # pragma: no cover - guarded by the parser
        raise InputError("unsupported period: %r" % (period,))
    start = date(start_year, start_month, 1)
    end_year, end_month = _add_months(start_year, start_month, length)
    return start, date(end_year, end_month, 1)


def _parse_period_key(period: str, key: str) -> date:
    try:
        if period == "month":
            return date.fromisoformat(key + "-01")
        if period == "quarter":
            year_text, quarter_text = key.split("-Q")
            quarter = int(quarter_text)
            if not 1 <= quarter <= 4:
                raise ValueError
            return date(int(year_text), (quarter - 1) * 3 + 1, 1)
        if period == "year":
            return date(int(key), 1, 1)
    except (ValueError, TypeError) as exc:
        raise InputError("invalid period key %r for period %r" % (key, period)) from exc
    raise InputError("unsupported period: %r" % (period,))


def period_for(instant: datetime, config) -> Period:
    local = instant.astimezone(ZoneInfo(config.timezone_name))
    start, end = _period_bounds(config.period, local.date())
    start_local = datetime(start.year, start.month, start.day, tzinfo=local.tzinfo)
    end_local = datetime(end.year, end.month, end.day, tzinfo=local.tzinfo)
    if config.period == "month":
        key = "%04d-%02d" % (start.year, start.month)
    elif config.period == "quarter":
        key = "%04d-Q%d" % (start.year, (start.month - 1) // 3 + 1)
    else:
        key = "%04d" % (start.year,)
    return Period(
        key=key,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(instant.tzinfo),
        end_utc=end_local.astimezone(instant.tzinfo),
    )


def contains(period: Period, instant: datetime) -> bool:
    return period.start_utc <= instant < period.end_utc


def period_bounds_for_key(period_name: str, key: str, timezone_name: str) -> Period:
    start = _parse_period_key(period_name, key)
    if period_name == "month":
        _, end = _period_bounds("month", start)
    elif period_name == "quarter":
        year, month = _add_months(start.year, start.month, 3)
        end = date(year, month, 1)
    else:
        end = date(start.year + 1, 1, 1)
    zone = ZoneInfo(timezone_name)
    start_local = datetime(start.year, start.month, start.day, tzinfo=zone)
    end_local = datetime(end.year, end.month, end.day, tzinfo=zone)
    return Period(
        key=key,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(zone),
        end_utc=end_local.astimezone(zone),
    )
