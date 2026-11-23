"""Session resume limit and deadline (SPEC 6.0.0 SES-2, SES-8, SES-9; issue #256 Part E).

Schema:
- session_resume_limit (append-only): an Administrator's revisions of the organization's resume
  limit, whole hours from 1 to 24. The latest revision applies; without one, 6 hours.
- inspection_session.resume_limit_hours: the limit captured when the session opened, an immutable
  creation fact. Every existing session gets 6 hours (SPEC 18.3 item 3).
- session_projection.ends_at: the session's end instant, for the job's query. The projection is
  a rebuildable cache; existing rows are filled from their own last activity.
- session_event gains the job's DEADLINE_INCOMPLETE event (no actor, not activity).

Definitions, identical in Python (cya.inspections.projections.fold_session) and in SQL here:
- A, the last persisted activity, is the latest effective_at of an event that carries an activity
  receipt (OPEN and REOPEN; JOIN and END never do, by the session_event_activity_shape CHECK) or
  the latest recorded_at of a check made in the session, whichever is later. This is the 0005
  auto-close and 0006 in-progress definition, unchanged.
- R = opened_at + resume_limit_hours; the effective deadline R* = GREATEST(R, A); the end instant
  E = LEAST(A + 6 h, R*).

Guards (each trigger rewritten from its previous text by exact replacements, each anchor asserted
to occur once; the reverse restores the previous text):
- inspection_session insert: the captured limit equals the organization's current one.
- session_event insert: AUTO_INCOMPLETE only at A + 6 h and only when that is not after R*;
  DEADLINE_INCOMPLETE only at R* and only when A + 6 h is after it; no JOIN or REOPEN at or after
  R*; the 0006 rule "another session of this vehicle checklist is in progress" judges the other
  session by its end instant E instead of A + 6 h.
- session_resume_limit insert: receipt attribution and consecutive revisions.
The check_record guard (no check at or after R*) is cya_checks 0019.

Reversing stops while a DEADLINE_INCOMPLETE event exists: a recorded ending is never discarded.
"""

from importlib import import_module

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

import cya.accounts.fields
from cya.accounts.fields import UUID_PATTERN

guards = import_module("cya.inspections.migrations.0003_session_guards")
previous = import_module("cya.inspections.migrations.0006_one_session_per_checklist")
signal = guards.signal
FLOOR = previous.FLOOR
ORG = "organization_id"
LIMITS = "session_resume_limit"
DEFAULT_HOURS = 6  # a migration states its own constants
LIMIT_FKS = (("command_id", "command_receipt"), ("actor_user_id", "user"))


def _replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"trigger anchor moved: {old[:60]!r}")
    return text.replace(old, new)


# ---- inspection_session: capture the organization's current limit --------------------------

CURRENT_LIMIT = (
    f"COALESCE((SELECT l.hours FROM {LIMITS} l WHERE l.{ORG}=NEW.{ORG} "
    f"ORDER BY l.revision DESC LIMIT 1), {DEFAULT_HOURS})"
)
SESSION_ANCHOR = f"THEN {signal('session version is not published at open')}; END IF;\nEND"


def session_insert():
    return _replace(
        guards.SESSION_INSERT,
        SESSION_ANCHOR,
        SESSION_ANCHOR[: -len("END")]
        + f"IF NEW.resume_limit_hours <> {CURRENT_LIMIT}\n"
        + f"THEN {signal('session resume limit is not the current limit')}; END IF;\nEND",
    )


HEADER_COLUMNS = (*guards.HEADER_COLUMNS, "resume_limit_hours")


def header_guard(columns):
    change = " OR ".join(f"NOT (BINARY OLD.{c} <=> BINARY NEW.{c})" for c in columns)
    return (
        "CREATE TRIGGER cya_inspection_session_immutable_bu BEFORE UPDATE ON inspection_session "
        f"FOR EACH ROW BEGIN IF {change} THEN {signal('immutable session header')}; "
        "END IF; END"
    )


# ---- session_event: the end instant ---------------------------------------------------------


def activity(session):
    """A for the session whose id is the SQL expression `session` (0005's definition)."""
    return (
        "GREATEST(\n"
        f"  COALESCE((SELECT MAX(a.effective_at) FROM session_event a WHERE a.session_id={session}\n"
        f"   AND a.persisted_activity_command_id IS NOT NULL), {FLOOR}),\n"
        f"  COALESCE((SELECT MAX(c.recorded_at) FROM check_record c WHERE c.session_id={session}),\n"
        f"   {FLOOR}))"
    )


DECLARATIONS = f"""FOR EACH ROW BEGIN
DECLARE v_activity DATETIME(6);
DECLARE v_deadline DATETIME(6);
SET v_activity = {activity("NEW.session_id")};
SET v_deadline = GREATEST((SELECT s.opened_at + INTERVAL s.resume_limit_hours HOUR
 FROM inspection_session s WHERE s.id=NEW.session_id), v_activity);
"""

OLD_SERVICE = "IF NEW.event_type='AUTO_INCOMPLETE' THEN"
NEW_SERVICE = "IF NEW.event_type IN ('AUTO_INCOMPLETE','DEADLINE_INCOMPLETE') THEN"

INACTIVITY_SIGNAL = signal("auto-close instant is not the last activity plus the inactivity limit")
NEW_INSTANTS = f""" IF NEW.event_type='AUTO_INCOMPLETE' AND (NEW.effective_at <> v_activity
  + INTERVAL {{hours}} HOUR OR v_activity + INTERVAL {{hours}} HOUR > v_deadline)
 THEN {INACTIVITY_SIGNAL}; END IF;
 IF NEW.event_type='DEADLINE_INCOMPLETE' AND (NEW.effective_at <> v_deadline
  OR v_activity + INTERVAL {{hours}} HOUR <= v_deadline)
 THEN {signal("deadline close instant is not the effective resume deadline")}; END IF;
"""

PAST_DEADLINE = f"""IF NEW.event_type IN ('JOIN','REOPEN') AND NEW.recorded_at >= v_deadline
THEN {signal("session has reached its resume deadline")}; END IF;
"""
ONE_IN_PROGRESS_START = "IF NEW.event_type IN ('OPEN','REOPEN') THEN\n SELECT v.id INTO"

ENDS = "'END_CLOSED','END_INCOMPLETE','AUTO_INCOMPLETE')"
REOPEN_PRIORS = "'END_INCOMPLETE',\n    'AUTO_INCOMPLETE'))))"


def event_insert(hours):
    sql = previous.event_insert(hours)
    sql = _replace(sql, "FOR EACH ROW BEGIN\n", DECLARATIONS)
    sql = _replace(sql, OLD_SERVICE, NEW_SERVICE)
    old_instant = (
        f" IF {previous.previous.AUTO_INSTANT.format(hours=hours)}\n THEN {INACTIVITY_SIGNAL}; "
        "END IF;\n"
    )
    sql = _replace(sql, old_instant, NEW_INSTANTS.format(hours=hours))
    sql = _replace(sql, ONE_IN_PROGRESS_START, PAST_DEADLINE + ONE_IN_PROGRESS_START)
    other = activity("o.id")
    sql = _replace(
        sql,
        f"AND NEW.recorded_at < {other} + INTERVAL {hours} HOUR)",
        f"AND NEW.recorded_at < LEAST({other} + INTERVAL {hours} HOUR,\n"
        f"  GREATEST(o.opened_at + INTERVAL o.resume_limit_hours HOUR, {other})))",
    )
    sql = _replace(sql, ENDS, ENDS[:-1] + ",'DEADLINE_INCOMPLETE')")
    return _replace(sql, REOPEN_PRIORS, REOPEN_PRIORS[:-4] + ",'DEADLINE_INCOMPLETE'))))")


# ---- session_resume_limit -------------------------------------------------------------------

LIMIT_GUARD = f"""CREATE TRIGGER cya_session_resume_limit_bi BEFORE INSERT ON {LIMITS}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='SESSION_RESUME_LIMIT_SET')
THEN {signal("resume limit receipt attribution mismatch")}; END IF;
IF NEW.revision <> 1 + (SELECT COUNT(*) FROM {LIMITS} l WHERE l.{ORG}=NEW.{ORG})
THEN {signal("resume limit revision mismatch")}; END IF;
END"""


def limit_statements():
    return [
        f"ALTER TABLE `{LIMITS}` ADD CONSTRAINT `cya_sess_{LIMITS}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{LIMITS}` ADD CONSTRAINT `cya_sess_{LIMITS}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{guards.ROLE_CODES}', actor_roles_at_time))",
        *(
            f"ALTER TABLE `{LIMITS}` ADD CONSTRAINT `cya_sess_{LIMITS}_{column}_org_fk` "
            f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
            for column, parent in LIMIT_FKS
        ),
        # Same statements the audit schema inspector (cya.core.audit_schema) requires on A tables.
        *(
            f"CREATE TRIGGER cya_{LIMITS}_{suffix} BEFORE {operation} ON {LIMITS} "
            f"FOR EACH ROW {signal('append-only table')}"
            for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
        ),
        LIMIT_GUARD,
    ]


def reverse_limit_statements():
    return [
        "DROP TRIGGER cya_session_resume_limit_bi",
        f"DROP TRIGGER cya_{LIMITS}_bd",
        f"DROP TRIGGER cya_{LIMITS}_bu",
        *(
            f"ALTER TABLE `{LIMITS}` DROP FOREIGN KEY `cya_sess_{LIMITS}_{column}_org_fk`"
            for column, _parent in reversed(LIMIT_FKS)
        ),
        f"ALTER TABLE `{LIMITS}` DROP CONSTRAINT `cya_sess_{LIMITS}_roles`",
        f"ALTER TABLE `{LIMITS}` DROP CONSTRAINT `cya_sess_{LIMITS}_uuid`",
    ]


def forward_statements():
    return [
        *limit_statements(),
        "DROP TRIGGER cya_inspection_session_bi",
        session_insert(),
        "DROP TRIGGER cya_inspection_session_immutable_bu",
        header_guard(HEADER_COLUMNS),
        "DROP TRIGGER cya_session_event_bi",
        event_insert(6),
    ]


def reverse_statements():
    return [
        "DROP TRIGGER cya_session_event_bi",
        previous.event_insert(6),
        "DROP TRIGGER cya_inspection_session_immutable_bu",
        guards.HEADER_GUARD,
        "DROP TRIGGER cya_inspection_session_bi",
        guards.SESSION_INSERT,
        *reverse_limit_statements(),
    ]


# The projection's end instant from its own last activity (it is rebuilt on every session event
# and every check in the session, so last_activity_at is A); a stale row only offers a session
# to the job early, and the job re-reads history.
FILL_ENDS_AT = """UPDATE session_projection p JOIN inspection_session s ON s.id=p.session_id
SET p.ends_at = LEAST(p.last_activity_at + INTERVAL 6 HOUR,
 GREATEST(s.opened_at + INTERVAL s.resume_limit_hours HOUR, p.last_activity_at))"""


def refuse_reverse_with_deadline_ends(apps, schema_editor):
    event = apps.get_model("cya_inspections", "SessionEvent")
    if event.objects.filter(event_type="DEADLINE_INCOMPLETE").exists():
        raise RuntimeError(
            "a session ended at its resume deadline; reversing would discard that record"
        )


EVENT_TYPES_BEFORE = ("OPEN", "JOIN", "REOPEN", "END_INCOMPLETE", "END_CLOSED", "AUTO_INCOMPLETE")
EVENT_TYPES = (*EVENT_TYPES_BEFORE, "DEADLINE_INCOMPLETE")
SERVICE_EVENTS = ("AUTO_INCOMPLETE", "DEADLINE_INCOMPLETE")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_inspections", "0006_one_session_per_checklist"),
    ]

    operations = [
        migrations.CreateModel(
            name="SessionResumeLimit",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("hours", models.PositiveSmallIntegerField()),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
            ],
            options={
                "db_table": "session_resume_limit",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="resume_limit_org_id"
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "revision"), name="resume_limit_revision"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gte", 1)), name="resume_limit_rev"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("hours__gte", 1), ("hours__lte", 24)),
                        name="resume_limit_hours",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="inspectionsession",
            name="resume_limit_hours",
            field=models.PositiveSmallIntegerField(default=DEFAULT_HOURS),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="inspectionsession",
            constraint=models.CheckConstraint(
                condition=models.Q(("resume_limit_hours__gte", 1), ("resume_limit_hours__lte", 24)),
                name="session_resume_limit_hours",
            ),
        ),
        migrations.AddField(
            model_name="sessionprojection",
            name="ends_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.RunSQL(FILL_ENDS_AT, reverse_sql=migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name="sessionevent", name="session_event_type"),
        migrations.RemoveConstraint(model_name="sessionevent", name="session_event_activity_shape"),
        migrations.RemoveConstraint(model_name="sessionevent", name="session_event_actor_shape"),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("event_type__in", EVENT_TYPES)), name="session_event_type"
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("event_type__in", ["OPEN", "REOPEN"]),
                        ("persisted_activity_command__isnull", False),
                    ),
                    models.Q(
                        (
                            "event_type__in",
                            [
                                "JOIN",
                                "END_INCOMPLETE",
                                "END_CLOSED",
                                "AUTO_INCOMPLETE",
                                "DEADLINE_INCOMPLETE",
                            ],
                        ),
                        ("persisted_activity_command__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="session_event_activity_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("actor_user__isnull", True), ("event_type__in", SERVICE_EVENTS)),
                    models.Q(
                        models.Q(("event_type__in", SERVICE_EVENTS), _negated=True),
                        ("actor_user__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="session_event_actor_shape",
            ),
        ),
        migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements()),
        # Last, so that a reverse checks first, before anything is undone.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_deadline_ends),
    ]
