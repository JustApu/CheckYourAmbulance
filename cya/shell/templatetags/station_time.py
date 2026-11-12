"""Render aware instants in the active agency zone, including DST folds."""

from datetime import datetime

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def station_time(value, precision="minutes"):
    """Format an aware instant without Django's ambiguous-time suppression."""
    if not isinstance(value, datetime) or timezone.is_naive(value):
        return ""
    local = value.astimezone(timezone.get_current_timezone())
    if precision == "seconds":
        pattern = "%Y-%m-%d %H:%M:%S %Z (%z)"
    else:
        pattern = "%Y-%m-%d %H:%M %Z (%z)"
    return local.strftime(pattern)
