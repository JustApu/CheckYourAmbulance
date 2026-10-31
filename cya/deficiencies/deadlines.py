"""The governing deadline, overdue state and age of a deficiency, derived from its events.

SPEC DEF-5: a supervisor's decision on an extension request sets the date that governs overdue
notices; extensions are unlimited. DEF-6: age since the original report is tracked separately from
the deadline. AC-26: a past instant answers from the events recorded by then.

Nothing here reads the database or the clock: the functions take the events (oldest first) and the
instant to answer for, so a dashboard, a notification job and the as-of reader give one answer.

A deadline revision is an event that carries a deadline and is not an extension decision: the
report (which may carry the first one) and every DEADLINE_SET, and later the reopening deadline of
issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156). The deadline then governing is that of the newest event carrying one, which is the
newest revision or a decision made after it. A new revision therefore supersedes every extension
decided under an older one, and a request still undecided when a revision arrives can no longer be
decided (the commands refuse it and the database trigger repeats the rule); both stay in the
history, and an as-of answer from before the revision still returns the older governing date.
"""

from cya.deficiencies.transitions import ACTIVE_STATUSES


class InconsistentHistory(ValueError):
    """The events do not form a history the commands could have written."""


PENDING = "PENDING"
APPROVING = ("APPROVED", "DIFFERENT_DATE")


def deadline_state(events):
    """Base deadline, governing deadline and every extension request, replayed from the events.

    events are DeficiencyEvent-like (event_type, pk, sequence, deadline, proposed_deadline,
    decision, request_event_id, explanation, recorded_at, actor_user_id), oldest first.
    """
    base_id = base = governing = governing_id = None
    requests = {}
    for event in events:
        kind = event.event_type
        if event.deadline is not None and kind != "EXTENSION_DECIDED":
            base_id, base = str(event.pk), event.deadline
            governing, governing_id = event.deadline, str(event.pk)
            for request in requests.values():
                request["superseded"] = True
        if kind == "EXTENSION_REQUESTED":
            requests[str(event.pk)] = {
                "request_event_id": str(event.pk),
                "requested_at": event.recorded_at,
                "requested_by": str(event.actor_user_id),
                "reason": event.explanation,
                "proposed_deadline": event.proposed_deadline,
                "status": PENDING,
                "superseded": False,
                "decision_event_id": None,
                "decided_at": None,
                "decided_by": None,
                "decision_explanation": None,
                "decided_deadline": None,
            }
        elif kind == "EXTENSION_DECIDED":
            request = requests.get(str(event.request_event_id))
            if request is None or request["status"] != PENDING:
                raise InconsistentHistory(f"decision {event.pk} does not answer a pending request")
            request.update(
                status=event.decision,
                decision_event_id=str(event.pk),
                decided_at=event.recorded_at,
                decided_by=str(event.actor_user_id),
                decision_explanation=event.explanation,
                decided_deadline=event.deadline,
            )
            if event.deadline is not None:
                governing, governing_id = event.deadline, str(event.pk)
    listed = list(requests.values())
    return {
        "base_deadline": base,
        "base_event_id": base_id,
        "governing_deadline": governing,
        "governing_event_id": governing_id,
        "requests": listed,
        "extension_count": len(listed),
        "approved_count": sum(1 for r in listed if r["status"] in APPROVING),
    }


def is_overdue(governing_deadline, status, at):
    """Past the governing deadline while the deficiency is still being worked (DEF-5)."""
    return governing_deadline is not None and status in ACTIVE_STATUSES and at > governing_deadline


def age_seconds(reported_at, at):
    """Whole seconds since the original report, whatever the deadline or status (DEF-6)."""
    return max(0, int((at - reported_at).total_seconds()))


def highlight(rule, age_seconds, extension_count):
    """What the age rule in force marks for a deficiency of this age and extension count (DEF-5/6).

    rule is the revision in force at the instant answered for, or None when an administrator has
    configured nothing: then nothing is highlighted and age and extension count stay plain facts.
    age_level counts the thresholds the age has reached (0 to 5); either long-running test left
    null is off. Only the dashboard shows this; no notification follows from age (DEF-6).
    """
    if rule is None:
        return {"age_rule_revision": None, "age_level": None, "long_running": False}
    if not all(type(item) is int for item in rule.age_thresholds_days):
        raise InconsistentHistory(f"age rule revision {rule.revision} holds a malformed threshold")
    days = age_seconds // 86400
    long_running = (
        rule.long_running_age_days is not None and days >= rule.long_running_age_days
    ) or (
        rule.long_running_extensions is not None and extension_count >= rule.long_running_extensions
    )
    return {
        "age_rule_revision": rule.revision,
        "age_level": sum(1 for threshold in rule.age_thresholds_days if days >= threshold),
        "long_running": long_running,
    }
