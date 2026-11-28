"""Paper-entered checks (SPEC 8.0.0 §5.3, §7.2, §7.9 LATE-4 to LATE-6, §18.4; issue #9 Part B2).

check_record gains two nullable columns, both null for a live check:

- paper_entry_batch_id, an organization-pair reference to the paper-entry batch the check was
  copied under;
- performed_at, the batch's original performance instant, repeated on the check so compliance
  and ordering read it without a join.

The qualifying time of a check (SPEC 5.3 t_c) is COALESCE(performed_at, recorded_at). The check
insert trigger now judges publication, override, kit-content and kit-assignment effectiveness at
t_c, which for a live row is recorded_at exactly as before; knowledge (what was recorded by
the entry instant) is still judged at recorded_at. A paper row must match its batch (vehicle,
printed version, entry actor and performance instant), carry no session, be ROUTINE and not
be after its entry instant; a paper kit-content row must come from a kit-definition version
printed for the batch. A correction keeps its chain's batch and instant.

The link insert trigger judges the pre-cutover late limit at t_c and refuses a paper link whose
obligation window [E, L] does not hold the performance instant (SCH-2, SCH-6).

Reversal is refused once a paper check exists, as for the other check-scope migrations.
"""

from importlib import import_module

from django.db import migrations, models
from django.db.models.deletion import PROTECT

previous = import_module("cya.checks.migrations.0021_pin_kit_presence_assignment")
previous_link = import_module("cya.checks.migrations.0017_item_requirement_link_scope")

TC = "COALESCE(NEW.performed_at,NEW.recorded_at)"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


# (anchor, replacement): each anchor must occur exactly once.
CHECK_REPLACEMENTS = (
    (
        "p.checklist_version_id=NEW.checklist_version_id AND p.effective_at<=NEW.recorded_at)",
        f"p.checklist_version_id=NEW.checklist_version_id AND p.effective_at<={TC})",
    ),
    (
        "op.override_version_id=o.id AND op.effective_at<=NEW.recorded_at",
        f"op.override_version_id=o.id AND op.effective_at<={TC}",
    ),
    (
        "JOIN kit_definition_publication kp ON kp.version_id=kv.id AND kp.effective_at<=NEW.recorded_at",
        f"JOIN kit_definition_publication kp ON kp.version_id=kv.id AND kp.effective_at<={TC}",
    ),
    (
        " AND e.effective_at<=NEW.recorded_at AND e.recorded_at<=NEW.recorded_at",
        f" AND e.effective_at<={TC} AND e.recorded_at<=NEW.recorded_at",
    ),
    (
        "AND newer.sequence>e.sequence AND newer.effective_at<=NEW.recorded_at",
        f"AND newer.sequence>e.sequence AND newer.effective_at<={TC}",
    ),
    (
        "AND p.kit_id<=>NEW.kit_id AND p.session_id<=>NEW.session_id AND p.purpose=NEW.purpose)",
        "AND p.kit_id<=>NEW.kit_id AND p.session_id<=>NEW.session_id AND p.purpose=NEW.purpose"
        " AND p.paper_entry_batch_id<=>NEW.paper_entry_batch_id"
        " AND p.performed_at<=>NEW.performed_at)",
    ),
)

PAPER_CHECK = f"""IF (NEW.paper_entry_batch_id IS NULL) <> (NEW.performed_at IS NULL)
THEN {signal("paper check needs its batch and performance time")}; END IF;
IF NEW.paper_entry_batch_id IS NOT NULL AND (NEW.session_id IS NOT NULL
 OR NEW.purpose<>'ROUTINE' OR NEW.checklist_version_id IS NULL
 OR NEW.performed_at>NEW.recorded_at
 OR NOT EXISTS (SELECT 1 FROM paper_entry_batch b WHERE b.id=NEW.paper_entry_batch_id
  AND b.organization_id=NEW.organization_id AND b.vehicle_id=NEW.vehicle_id
  AND b.checklist_version_id=NEW.checklist_version_id AND b.actor_user_id=NEW.actor_user_id
  AND b.performed_at=NEW.performed_at AND b.recorded_at<=NEW.recorded_at))
THEN {signal("paper check does not match its batch")}; END IF;
IF NEW.paper_entry_batch_id IS NOT NULL AND NEW.kit_definition_item_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM kit_definition_item i
  JOIN paper_entry_kit_version pk ON pk.kit_definition_version_id=i.version_id
  WHERE i.id=NEW.kit_definition_item_id AND pk.batch_id=NEW.paper_entry_batch_id)
THEN {signal("paper kit check needs its printed kit version")}; END IF;
"""

LINK_LATE = "o.kit_key='00000000-0000-0000-0000-000000000000' AND NEW.recorded_at<=o.late_limit_at"
PAPER_LINK = (
    " IF EXISTS (SELECT 1 FROM check_record pc JOIN obligation po ON po.id=NEW.obligation_id"
    " WHERE pc.id=NEW.check_id AND pc.performed_at IS NOT NULL"
    " AND NOT (po.early_at<=pc.performed_at AND pc.performed_at<=po.late_limit_at))"
    f" THEN {signal('paper check link outside the obligation window')}; END IF;"
)


def _replace_once(sql, anchor, replacement, what):
    if sql.count(anchor) != 1:
        raise ValueError(f"{what} anchor moved: {anchor[:60]}")
    return sql.replace(anchor, replacement)


def check_trigger():
    sql = previous.check_trigger()
    for anchor, replacement in CHECK_REPLACEMENTS:
        sql = _replace_once(sql, anchor, replacement, "check trigger")
    if not sql.rstrip().endswith("END"):
        raise ValueError("check trigger no longer ends with END")
    body = sql.rstrip()[: -len("END")]
    return body + PAPER_CHECK + "END"


def link_trigger():
    sql = previous_link.link_trigger()
    sql = _replace_once(
        sql,
        LINK_LATE,
        "o.kit_key='00000000-0000-0000-0000-000000000000'"
        " AND COALESCE(c.performed_at,NEW.recorded_at)<=o.late_limit_at",
        "link trigger",
    )
    if not sql.rstrip().endswith(" END"):
        raise ValueError("link trigger no longer ends with END")
    return sql.rstrip()[: -len(" END")] + PAPER_LINK + " END"


def refuse_reverse_with_paper_checks(apps, schema_editor):
    Check = apps.get_model("cya_checks", "CheckRecord")
    if Check.objects.filter(paper_entry_batch__isnull=False).exists():
        raise RuntimeError("cannot reverse paper checks while append-only paper checks exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0021_pin_kit_presence_assignment"),
        ("cya_paper", "0002_paper_entry_guards"),
    ]

    # The reverse guard is the last operation, so on unapply it runs first, while the columns
    # still exist; MariaDB DDL is not transactional, so a later refusal would leave it half done.
    operations = [
        migrations.AddField(
            model_name="checkrecord",
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
            model_name="checkrecord",
            name="performed_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(
            [
                "ALTER TABLE check_record ADD CONSTRAINT cya_check_record_paper_batch_org_fk "
                "FOREIGN KEY (organization_id,paper_entry_batch_id) "
                "REFERENCES paper_entry_batch (organization_id,id) "
                "ON DELETE RESTRICT ON UPDATE RESTRICT",
                "DROP TRIGGER cya_check_record_bi",
                check_trigger(),
                "DROP TRIGGER cya_check_obligation_link_bi",
                link_trigger(),
            ],
            reverse_sql=[
                "DROP TRIGGER cya_check_obligation_link_bi",
                previous_link.link_trigger(),
                "DROP TRIGGER cya_check_record_bi",
                previous.check_trigger(),
                "ALTER TABLE check_record DROP FOREIGN KEY cya_check_record_paper_batch_org_fk",
                "ALTER TABLE check_record DROP INDEX cya_check_record_paper_batch_org_fk",
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_paper_checks),
    ]
