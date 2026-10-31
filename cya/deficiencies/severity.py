"""Severity levels: the defaults an organization starts with, and resolution by code (DEF-8).

The levels are data (deficiency_severity), so an administrator can add, reword, reorder or retire
one by appending a revision; this module only seeds the defaults and reads the level in force.
The criteria below are the owner-approved starting wording (SPEC 8.3, OQ-56). Severity informs
people only: it never changes a vehicle's operational status (OPS-2).
"""

from django.db import transaction

from cya.deficiencies.models import DeficiencySeverity

DEFAULT_LEVELS = (
    (
        "LOW",
        "Low",
        "Cosmetic or minor. Does not affect patient care or safe operation. Fix at convenience.",
    ),
    (
        "MEDIUM",
        "Medium",
        "Reduces convenience or a safety margin, and a workaround exists. Fix in normal scheduling.",
    ),
    (
        "HIGH",
        "High",
        "Impairs equipment or capability needed on a call. Fix soon; a supervisor should review it.",
    ),
    (
        "CRITICAL",
        "Critical",
        "Prevents safe patient care or safe driving, or is a safety or regulatory failure. "
        "Tell a supervisor now.",
    ),
)


def ensure_default_severities(organization_id, effective_at):
    """Insert the default levels where the organization has none; idempotent, never overwrites."""
    with transaction.atomic():
        created = 0
        for rank, (code, label, description) in enumerate(DEFAULT_LEVELS, start=1):
            _row, was_created = DeficiencySeverity.objects.get_or_create(
                organization_id=organization_id,
                code=code,
                revision=1,
                defaults={
                    "label": label,
                    "description": description,
                    "rank": rank,
                    "active": True,
                    "effective_at": effective_at,
                },
            )
            created += was_created
    return created


def levels_in_force(organization_id, now):
    """The newest revision of each level effective at now, retired ones included."""
    newest = {}
    rows = DeficiencySeverity.objects.filter(
        organization_id=organization_id, effective_at__lte=now
    ).order_by("code", "revision")
    for row in rows:
        newest[row.code] = row
    return newest


def active_levels(organization_id, now):
    """The levels a reporter may choose now, lowest rank (least severe) first."""
    levels = [r for r in levels_in_force(organization_id, now).values() if r.active]
    return sorted(levels, key=lambda r: (r.rank, r.code))


def resolve_severity(organization_id, code, now):
    """The revision in force for an active level, else None (unknown, future or retired)."""
    level = levels_in_force(organization_id, now).get(code)
    return level if level is not None and level.active else None
