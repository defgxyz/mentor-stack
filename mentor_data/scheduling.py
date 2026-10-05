"""Shared cron-based scheduling: lets any per-item due-time (objective
follow-ups, service checks, topic check-ins) optionally use a real 5-field
cron expression ("0 9 * * 1" = every Monday at 9am, "0 9 * * 1,2" = every
Monday and Tuesday at 9am) instead of a flat "every N days" interval - via
croniter, a small, standard library (not hand-rolled recurrence math).

A day-count interval is still the default and stays fully supported - this
is purely additive, for when someone wants a specific day/time rather than
"every 7 days from whenever it last fired" (which drifts across weekdays
over time, unlike a real cron expression).
"""
from datetime import datetime

from croniter import croniter


def validate_cron(expr: str) -> bool:
    """True if expr is a real, parseable 5-field cron expression."""
    try:
        return bool(expr) and croniter.is_valid(expr)
    except Exception:
        return False


def next_from_cron(expr: str, from_time: datetime) -> str:
    """The next fire time strictly after from_time, as an ISO string."""
    return croniter(expr, from_time).get_next(datetime).isoformat()
