"""Prepare a rehearsal database for one step of the shift-change load test (issue #10).

Runs inside Django, with the schema-owner environment of a **rehearsal** database that already
holds one synthetic agency (for example the issue #7 history). For a scale factor S it makes sure
the agency has 5×S vehicles, 3 crew per vehicle and 2×S supervisors (all synthetic), publishes a
new daily schedule slot whose on-time window is open during the step, binds every vehicle to it,
generates obligations, and mints one session per user.

Sessions are minted with Django's session store, which is what sign-in does after Google
accepts a user (`cya.accounts.views`); the load users never sign in through Google. That is
only acceptable for synthetic users on a throwaway host, so this refuses any database whose
name does not end in `_rehearsal`. The returned plan holds live session cookies: keep it out
of logs and shared storage, and pass it straight to the load client.

    python manage.py shell -c "from tools.load_prepare import prepare_json; print(prepare_json(4))"
"""

import json
import re
from datetime import UTC, timedelta
from uuid import uuid4

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.db import connection, transaction
from django.utils import timezone

from cya.accounts.middleware import SESSION_AUTH_TIME_KEY, SESSION_USER_KEY
from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.authorization.roles import grant_initial_crew
from cya.checklists.models import ChecklistTemplate, Compartment, Vehicle
from cya.obligations.commands import bind_requirement
from cya.obligations.generation import run_generation
from cya.schedules.commands import publish_rule
from cya.schedules.engine import Rule

VEHICLES_PER_STEP = 5
CREW_PER_VEHICLE = 3
SUPERVISORS_PER_STEP = 2
MAX_SCALE = 20
LEAD = timedelta(
    minutes=50
)  # the slot is due this long after preparation; its window opens 1 h early


def _refuse_unless_rehearsal():
    name = connection.settings_dict["NAME"]
    if not name.endswith("_rehearsal"):
        raise RuntimeError(f"refusing database {name!r}: load preparation is for *_rehearsal only")


def _context(org, actor):
    return {
        "organization_id": str(org.pk),
        "actor_id": str(actor.pk),
        "operation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "request_attempt_id": str(uuid4()),
    }


def _user(org, name):
    email = f"rehearsal-{name}@example.test"
    found = User.objects.filter(organization=org, email=email).first()
    if found:
        return found, False
    user = User.objects.create(
        organization=org,
        idp_issuer="https://accounts.google.com",
        idp_subject=f"synthetic-rehearsal-{name}",
        email=email,
        display_name=f"Rehearsal {name} Synthetic",
    )
    with transaction.atomic():
        grant_initial_crew(user=user, command_id=str(uuid4()))
    return user, True


def _unit_number(vehicle):
    found = re.search(r"unit (\d+)", vehicle.display_name)
    return (int(found.group(1)) if found else 10**6, str(vehicle.pk))


def _vehicles(org, count, now):
    """The first `count` rehearsal vehicles, creating any that are missing."""
    existing = sorted(Vehicle.objects.filter(organization=org), key=_unit_number)
    if not existing:
        raise RuntimeError("the rehearsal agency has no vehicles; generate its history first")
    template_compartment = Compartment.objects.filter(vehicle=existing[0]).order_by("pk").first()
    created = 0
    for n in range(len(existing) + 1, count + 1):
        vehicle = Vehicle.objects.create(
            organization=org, display_name=f"Rehearsal unit {n} Synthetic"
        )
        Compartment.objects.create(
            organization=org,
            vehicle=vehicle,
            stable_slot_id=template_compartment.stable_slot_id,
            revision=1,
            label=template_compartment.label,
            effective_at=now - timedelta(days=1),
        )
        existing.append(vehicle)
        created += 1
    return existing[:count], created


def prepare(scale, *, now=None):
    """Prepare one step at `scale`; return the plan (users with cookies) and what was created."""
    _refuse_unless_rehearsal()
    if type(scale) is not int or not 1 <= scale <= MAX_SCALE:
        raise ValueError(f"scale must be an integer from 1 to {MAX_SCALE}")
    now = now or timezone.now()
    org = Organization.objects.get()
    admin = User.objects.get(organization=org, email="rehearsal-administrator@example.test")
    template = ChecklistTemplate.objects.get(organization=org)

    vehicles, vehicles_created = _vehicles(org, VEHICLES_PER_STEP * scale, now)
    crew = []
    users_created = 0
    for index in range(len(vehicles) * CREW_PER_VEHICLE):
        # The history's 15 crew come first, then load crew.
        name = f"crew-{index + 1:02d}" if index < 15 else f"load-crew-{index + 1:03d}"
        user, made = _user(org, name)
        crew.append(user)
        users_created += made
    supervisors = []
    for index in range(SUPERVISORS_PER_STEP * scale):
        name = "supervisor" if index == 0 else f"load-supervisor-{index + 1:02d}"
        user, made = _user(org, name)
        if made:
            change_role(
                target_id=str(user.pk), role="SUPERVISOR", action="GRANT", **_context(org, admin)
            )
        supervisors.append(user)
        users_created += made

    due = (now + LEAD).astimezone(UTC).replace(second=0, microsecond=0)
    slot = f"load-{due:%Y%m%d-%H%M}-{uuid4().hex[:8]}"  # a new slot per step, never a revision
    publish_rule(
        slot=slot,
        rule=Rule("UTC", due.time(), due.replace(hour=0, minute=0)),
        **_context(org, admin),
    )
    for vehicle in vehicles:
        bind_requirement(
            vehicle_id=str(vehicle.pk),
            template_id=str(template.pk),
            slot=slot,
            **_context(org, admin),
        )
    failures = run_generation(str(org.pk)).failures
    if failures:
        raise RuntimeError(f"obligation generation failed: {failures}")

    users = []
    for v_index, vehicle in enumerate(vehicles):
        for member in crew[v_index * CREW_PER_VEHICLE : (v_index + 1) * CREW_PER_VEHICLE]:
            users.append({"role": "crew", "vehicle_id": str(vehicle.pk), **_session(member, now)})
    for supervisor in supervisors:
        users.append({"role": "supervisor", **_session(supervisor, now)})
    return {
        "scale": scale,
        "slot": slot,
        "due_at": due.isoformat(),
        "template_id": str(template.pk),
        "vehicle_ids": [str(v.pk) for v in vehicles],
        "created": {"vehicles": vehicles_created, "users": users_created},
        "users": users,
    }


def _session(user, now):
    store = SessionStore()
    store[SESSION_USER_KEY] = str(user.pk)
    store[SESSION_AUTH_TIME_KEY] = int(now.timestamp())
    store.save()
    return {"user_id": str(user.pk), "cookie": store.session_key}


def prepare_json(scale):
    """`prepare` as one JSON line, for piping to the load client (never to a log)."""
    return json.dumps(prepare(scale), separators=(",", ":"))
