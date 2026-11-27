"""Paper seal verifications and paper seal breaks (SPEC 8.0.0 LATE-7, §10.1/§10.2; issue #9 B4).

seal_event and seal_substitution gain paper_entry_batch_id (an organization-pair reference) and
performed_at, both null for a live record. A record's qualifying instant is
COALESCE(performed_at, recorded_at).

Within one seal subject's stream, sequence order stays qualifying-instant order: the chain
predicate of seal_event now also requires the previous event's qualifying instant not to be
later than the new one's. A live event always satisfies that. A paper record may only be a
BREAK (no paper reseal or audit; owner question on issue #9) and is refused when a
substitution of the subject has a later qualifying instant, so it never rewrites later evidence.
Every reader that orders a stream by sequence therefore stays correct.

A paper substitution must rely on the seal intact at its performance instant: a RESEAL no later
than that instant, with no BREAK, RESEAL or AUDIT_REQUEST after it in the stream by then. Its
obligation links must hold the performance instant in their window (late links up to L). A
paper kit substitution is never nested evidence for a live cabinet inspection.

The reverse refusal is the last operation, so on unapply it runs first, while the columns exist.
"""

from importlib import import_module

from django.db import migrations, models
from django.db.models.deletion import PROTECT

guards = import_module("cya.seals.migrations.0002_evidence_guards")

QI = "COALESCE(NEW.performed_at,NEW.recorded_at)"
STREAM = (
    "{a}.kit_id<=>{b}.kit_id AND ({b}.kit_id IS NOT NULL OR ({a}.vehicle_id={b}.vehicle_id"
    " AND {a}.compartment_slot_id={b}.compartment_slot_id))"
)


def signal(message):
    return guards.signal(message)


def _replace_once(sql, anchor, replacement):
    if sql.count(anchor) != 1:
        raise ValueError(f"seal guard anchor moved: {anchor[:60]}")
    return sql.replace(anchor, replacement)


def _batch_match(event_type_rule):
    return f"""IF (NEW.paper_entry_batch_id IS NULL) <> (NEW.performed_at IS NULL)
THEN {signal("paper seal record needs its batch and performance time")}; END IF;
IF NEW.paper_entry_batch_id IS NOT NULL AND ({event_type_rule}NEW.performed_at>NEW.recorded_at
 OR NOT EXISTS (SELECT 1 FROM paper_entry_batch b WHERE b.id=NEW.paper_entry_batch_id
  AND b.organization_id=NEW.organization_id AND b.vehicle_id=NEW.vehicle_id
  AND b.actor_user_id=NEW.actor_user_id AND b.performed_at=NEW.performed_at
  AND b.recorded_at<=NEW.recorded_at))
THEN {signal("paper seal record does not match its batch")}; END IF;"""


def event_guard():
    sql = guards.EVENT_GUARD
    sql = _replace_once(
        sql,
        "AND c.stable_slot_id=NEW.compartment_slot_id AND c.effective_at<=NEW.recorded_at",
        f"AND c.stable_slot_id=NEW.compartment_slot_id AND c.effective_at<={QI}",
    )
    sql = _replace_once(
        sql, " AND n.effective_at<=NEW.recorded_at))", f" AND n.effective_at<={QI}))"
    )
    sql = _replace_once(
        sql,
        " AND p.kit_id <=> NEW.kit_id\n",
        f" AND p.kit_id <=> NEW.kit_id AND COALESCE(p.performed_at,p.recorded_at)<={QI}\n",
    )
    later = STREAM.format(a="e", b="NEW")
    return f"""{sql}
{_batch_match("NEW.event_type<>'BREAK' OR ")}
IF NEW.paper_entry_batch_id IS NOT NULL AND EXISTS (SELECT 1 FROM seal_substitution s
 JOIN seal_event e ON e.id=s.seal_event_id WHERE e.organization_id=NEW.organization_id
 AND {later} AND COALESCE(s.performed_at,s.recorded_at)>NEW.performed_at)
THEN {signal("paper seal break precedes later seal evidence")}; END IF;"""


def substitution_guard():
    later = STREAM.format(a="later", b="e")
    return f"""{guards.SUBSTITUTION_GUARD}
{_batch_match("")}
IF NEW.performed_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM seal_event e
 WHERE e.id=NEW.seal_event_id AND COALESCE(e.performed_at,e.recorded_at)<=NEW.performed_at
 AND NOT EXISTS (SELECT 1 FROM seal_event later WHERE later.organization_id=e.organization_id
  AND {later} AND later.sequence>e.sequence
  AND later.event_type IN ('BREAK','RESEAL','AUDIT_REQUEST')
  AND COALESCE(later.performed_at,later.recorded_at)<=NEW.performed_at))
THEN {signal("paper seal substitution needs the seal intact then")}; END IF;"""


def link_guard():
    return f"""{guards.LINK_GUARD}
IF EXISTS (SELECT 1 FROM seal_substitution s JOIN obligation o ON o.id=NEW.obligation_id
 WHERE s.id=NEW.substitution_id AND s.performed_at IS NOT NULL
 AND NOT (o.early_at<=s.performed_at AND (s.performed_at<=o.grace_at
  OR (NEW.late_eligible=1 AND o.late_limit_at IS NOT NULL AND s.performed_at<=o.late_limit_at))))
THEN {signal("paper seal link outside the obligation window")}; END IF;"""


def kit_verification_guard():
    return _replace_once(
        guards.KIT_VERIFICATION_GUARD,
        " AND s.recorded_at<=NEW.recorded_at\n",
        " AND s.recorded_at<=NEW.recorded_at AND s.performed_at IS NULL\n",
    )


REBUILT = (
    ("seal_event", "'SEAL_BREAK','SEAL_RESEAL','SEAL_AUDIT_REQUEST'", event_guard, "EVENT_GUARD"),
    ("seal_substitution", "'SEAL_SUBSTITUTE'", substitution_guard, "SUBSTITUTION_GUARD"),
    ("seal_substitution_obligation", "'SEAL_SUBSTITUTE'", link_guard, "LINK_GUARD"),
    (
        "seal_inspection_kit_verification",
        "'SEAL_RESEAL'",
        kit_verification_guard,
        "KIT_VERIFICATION_GUARD",
    ),
)
PAPER_TABLES = ("seal_event", "seal_substitution")


def forward_sql():
    statements = [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_{table}_paper_batch_org_fk` "
        "FOREIGN KEY (organization_id,paper_entry_batch_id) REFERENCES paper_entry_batch "
        "(organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT"
        for table in PAPER_TABLES
    ]
    for table, types, body, _old in REBUILT:
        statements += [f"DROP TRIGGER cya_{table}_bi", guards.insert_guard(table, types, body())]
    return statements


def reverse_sql():
    statements = []
    for table, types, _body, old in REBUILT:
        statements += [
            f"DROP TRIGGER cya_{table}_bi",
            guards.insert_guard(table, types, getattr(guards, old)),
        ]
    for table in PAPER_TABLES:
        statements += [
            f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_{table}_paper_batch_org_fk`",
            f"ALTER TABLE `{table}` DROP INDEX `cya_{table}_paper_batch_org_fk`",
        ]
    return statements


def refuse_reverse_with_paper_seals(apps, schema_editor):
    for name in ("SealEvent", "SealSubstitution"):
        if (
            apps.get_model("cya_seals", name)
            .objects.filter(paper_entry_batch__isnull=False)
            .exists()
        ):
            raise RuntimeError("cannot reverse paper seal records while they exist")


def _fields(model_name):
    return [
        migrations.AddField(
            model_name=model_name,
            name="paper_entry_batch",
            field=models.ForeignKey(
                db_constraint=False,
                null=True,
                on_delete=PROTECT,
                related_name="+",
                to="cya_paper.paperentrybatch",
            ),
        ),
        migrations.AddField(
            model_name=model_name, name="performed_at", field=models.DateTimeField(null=True)
        ),
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_seals", "0009_physical_presence_certification"),
        ("cya_paper", "0002_paper_entry_guards"),
    ]

    operations = [
        *_fields("sealevent"),
        *_fields("sealsubstitution"),
        migrations.RunSQL(forward_sql(), reverse_sql=reverse_sql()),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_paper_seals),
    ]
