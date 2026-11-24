"""Persist fresh per-unit presence observations in seal certifications."""

from django.db import migrations, models
from django.db.models.deletion import PROTECT

from cya.accounts.fields import UUID_PATTERN, IdentityIDField, new_id

ROLE_CODES = '["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]'
TABLE = "seal_inspection_kit_presence"
FKS = (
    ("command_id", "command_receipt"),
    ("actor_user_id", "user"),
    ("inspection_id", "seal_inspection"),
    ("kit_id", "kit_instance"),
    ("assignment_id", "kit_assignment_event"),
    ("item_id", "item"),
    ("check_record_id", "check_record"),
)

PRESENCE_GUARD = """IF NOT EXISTS (
 SELECT 1 FROM seal_inspection i JOIN seal_event subject
 ON subject.id=i.reseal_id AND subject.organization_id=i.organization_id
 JOIN command_receipt started ON started.id=i.start_receipt_id
 JOIN kit_assignment_event a ON a.id=NEW.assignment_id
 JOIN kit_instance_definition kd ON kd.kit_id=a.kit_id AND kd.organization_id=a.organization_id
 JOIN kit_definition d ON d.id=kd.definition_id AND d.organization_id=kd.organization_id
 JOIN compartment c ON c.id=a.compartment_id AND c.organization_id=a.organization_id
 JOIN check_record check_row ON check_row.id=NEW.check_record_id
 JOIN check_record root ON root.id=check_row.root_check_id
 WHERE i.id=NEW.inspection_id AND i.organization_id=NEW.organization_id
 AND subject.vehicle_id=a.vehicle_id AND a.organization_id=NEW.organization_id
 AND a.kit_id=NEW.kit_id AND a.active=1 AND a.compartment_id IS NOT NULL
 AND a.effective_at<=NEW.recorded_at AND a.recorded_at<=NEW.recorded_at
 AND d.presence_item_id=NEW.item_id
 AND (subject.kit_id=NEW.kit_id OR (subject.kit_id IS NULL
  AND c.stable_slot_id=subject.compartment_slot_id))
 AND NOT EXISTS (SELECT 1 FROM kit_assignment_event newer
  WHERE newer.organization_id=a.organization_id AND newer.kit_id=a.kit_id
  AND newer.sequence>a.sequence AND newer.effective_at<=NEW.recorded_at)
 AND started.organization_id=NEW.organization_id AND started.actor_user_id=NEW.actor_user_id
 AND started.command_type='SEAL_INSPECTION_START' AND started.recorded_at<root.recorded_at
 AND check_row.organization_id=NEW.organization_id AND check_row.actor_user_id=NEW.actor_user_id
 AND root.organization_id=NEW.organization_id AND root.actor_user_id=NEW.actor_user_id
 AND check_row.vehicle_id=subject.vehicle_id AND check_row.item_id=NEW.item_id
 AND check_row.kit_id=NEW.kit_id AND check_row.kit_assignment_event_id=NEW.assignment_id
 AND check_row.kit_presence=1 AND check_row.kit_requirement_id IS NULL
 AND check_row.quantity=NEW.count
 AND ((NEW.count=1 AND check_row.outcome='PASSED') OR
      (NEW.count=0 AND check_row.outcome='FAILED'))
 AND check_row.recorded_at<=NEW.recorded_at
 AND NOT EXISTS (SELECT 1 FROM check_event invalidation
  WHERE invalidation.organization_id=NEW.organization_id
  AND invalidation.root_check_id=check_row.root_check_id
  AND invalidation.event_type='INVALIDATION' AND invalidation.recorded_at<=NEW.recorded_at)
 AND NOT EXISTS (SELECT 1 FROM check_record successor
  WHERE successor.organization_id=NEW.organization_id
  AND successor.predecessor_check_id=check_row.id AND successor.recorded_at<=NEW.recorded_at))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'seal kit presence needs a fresh matching check'; END IF;"""


def insert_guard():
    return f"""CREATE TRIGGER cya_{TABLE}_bi BEFORE INSERT ON `{TABLE}`
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='SEAL_RESEAL')
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='seal evidence receipt attribution mismatch'; END IF;
{PRESENCE_GUARD}
END"""


def refuse_reverse_with_presence(apps, schema_editor):
    Presence = apps.get_model("cya_seals", "SealInspectionKitPresence")
    if Presence.objects.exists():
        raise RuntimeError(
            "cannot reverse physical kit presence certification while append-only evidence exists"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cya_seals", "0008_item_requirement_link_scope"),
        ("cya_checks", "0021_pin_kit_presence_assignment"),
        ("cya_kits", "0018_active_unassigned_kits"),
    ]

    operations = [
        migrations.CreateModel(
            name="SealInspectionKitPresence",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    IdentityIDField(
                        default=new_id, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("count", models.PositiveIntegerField()),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_accounts.user"
                    ),
                ),
                (
                    "assignment",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=PROTECT,
                        related_name="+",
                        to="cya_kits.kitassignmentevent",
                    ),
                ),
                (
                    "check_record",
                    models.ForeignKey(
                        db_constraint=False,
                        on_delete=PROTECT,
                        related_name="+",
                        to="cya_checks.checkrecord",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_core.commandreceipt"
                    ),
                ),
                (
                    "inspection",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_seals.sealinspection"
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_checklists.item"
                    ),
                ),
                (
                    "kit",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_checklists.kitinstance"
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=PROTECT, related_name="+", to="cya_accounts.organization"
                    ),
                ),
            ],
            options={"db_table": TABLE},
        ),
        migrations.AddConstraint(
            model_name="sealinspectionkitpresence",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="sealkitpresence_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="sealinspectionkitpresence",
            constraint=models.UniqueConstraint(
                fields=("inspection", "kit"), name="sealkitpresence_kit"
            ),
        ),
        migrations.AddConstraint(
            model_name="sealinspectionkitpresence",
            constraint=models.CheckConstraint(
                condition=models.Q(count__in=[0, 1]), name="sealkitpresence_count"
            ),
        ),
        migrations.RunSQL(
            [
                f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
                f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
                *[
                    f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` FOREIGN KEY (organization_id,{column}) REFERENCES `{parent}` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT"
                    for column, parent in FKS
                ],
                f"CREATE TRIGGER cya_{TABLE}_bu BEFORE UPDATE ON `{TABLE}` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                f"CREATE TRIGGER cya_{TABLE}_bd BEFORE DELETE ON `{TABLE}` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                insert_guard(),
            ],
            reverse_sql=[
                f"DROP TRIGGER cya_{TABLE}_bi",
                f"DROP TRIGGER cya_{TABLE}_bd",
                f"DROP TRIGGER cya_{TABLE}_bu",
                *[
                    f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
                    for column, _ in reversed(FKS)
                ],
                f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
                f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_presence),
    ]
