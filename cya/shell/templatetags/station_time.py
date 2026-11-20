"""Render aware instants in station time (issue #235 C1, owner decision 2026-11-19).

An organization works in one time zone, so a time is shown without a zone name or offset. The
one exception is the hour a fall-back transition repeats: there the same wall time names two
instants, so it carries the zone abbreviation ("01:30 EDT", then "01:30 EST").
"""

from datetime import datetime

from django import template
from django.utils import timezone

register = template.Library()


def repeated_wall_time(local):
    """True when this local time occurs twice (the repeated hour of a fall-back transition)."""
    return local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset()


@register.filter
def station_time(value, precision="minutes"):
    """Format an aware instant in the active station zone, without Django's fold suppression."""
    if not isinstance(value, datetime) or timezone.is_naive(value):
        return ""
    local = value.astimezone(timezone.get_current_timezone())
    pattern = "%Y-%m-%d %H:%M:%S" if precision == "seconds" else "%Y-%m-%d %H:%M"
    if repeated_wall_time(local):
        pattern += " %Z"
    return local.strftime(pattern)
