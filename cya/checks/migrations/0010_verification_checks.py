"""Verification checks name the Pending Verification deficiency they verify (issue 154 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/154)).

check_record.verification_for_deficiency_id (SPEC VER-4, VER-6) is set exactly when purpose is
VERIFICATION, which a CHECK enforces. The extended insert trigger repeats the part the database can
know on its own, so a writer that bypasses record_check still cannot record a verification check
for a deficiency of another vehicle or one that is not Pending Verification, for an item outside
the deficiency's newest scope revision, or with an outcome that is neither Passed nor Failed. A
correction row must repeat its chain's target. Whether the check counts and whether it closes the
deficiency is decided by the command and re-verified by the deficiency event trigger.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

prior = import_module("cya.checks.migrations.0006_check_corrections")
base = import_module("cya.checks.migrations.0003_check_session")
signal, ORG = base.signal, base.ORG

NO_KIT = "00000000-0000-0000-0000-000000000000"

VERIFICATION_RULES = f"""IF NEW.predecessor_check_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM check_record p
 WHERE p.id=NEW.predecessor_check_id AND p.{ORG}=NEW.{ORG}
 AND p.verification_for_deficiency_id <=> NEW.verification_for_deficiency_id
 AND p.verification_scope_revision <=> NEW.verification_scope_revision)
THEN {signal("check correction must keep its verification target")}; END IF;
IF NEW.predecessor_check_id IS NULL AND NEW.verification_for_deficiency_id IS NOT NULL AND (
 NEW.outcome NOT IN ('PASSED','FAILED')
 OR NOT EXISTS (SELECT 1 FROM deficiency d WHERE d.id=NEW.verification_for_deficiency_id
  AND d.{ORG}=NEW.{ORG} AND d.vehicle_id=NEW.vehicle_id)
 OR IFNULL((SELECT e.to_status FROM deficiency_event e
  WHERE e.deficiency_id=NEW.verification_for_deficiency_id ORDER BY e.sequence DESC LIMIT 1),'')
  <>'PENDING_VERIFICATION'
 OR NOT EXISTS (SELECT 1 FROM verification_scope_row s
  WHERE s.deficiency_id=NEW.verification_for_deficiency_id AND s.{ORG}=NEW.{ORG}
  AND s.item_id=NEW.item_id AND BINARY s.instance_key=BINARY IFNULL(NEW.kit_id,'{NO_KIT}')
  AND s.scope_revision=NEW.verification_scope_revision
  AND s.scope_revision=(SELECT MAX(x.scope_revision) FROM verification_scope_row x
   WHERE x.deficiency_id=s.deficiency_id)))
THEN {signal("verification check is not for a pending scope item of this vehicle")}; END IF;
"""


def check_insert():
    trigger = prior._check_insert().rstrip()
    assert trigger.endswith("END")
    return trigger[: -len("END")] + VERIFICATION_RULES + "END"


FORWARD = [
    "ALTER TABLE `check_record` ADD CONSTRAINT `cya_check_record_verification_deficiency_org_fk` "
    f"FOREIGN KEY ({ORG},verification_for_deficiency_id) REFERENCES `deficiency` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT",
    "DROP TRIGGER cya_check_record_bi",
    check_insert(),
]
REVERSE = [
    "DROP TRIGGER cya_check_record_bi",
    prior._check_insert(),
    "ALTER TABLE `check_record` DROP FOREIGN KEY `cya_check_record_verification_deficiency_org_fk`",
    # As in 0005, a dumped FK index must go before Django drops its reference column.
    "DROP INDEX IF EXISTS `cya_check_record_verification_deficiency_org_fk` ON `check_record`",
]


def refuse_reverse_with_data(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM check_record WHERE purpose='VERIFICATION'")
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse verification checks while such checks exist")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0003_publication_command_type_guards"),
        ("cya_checks", "0009_unable_check_chain_effects"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_deficiencies", "0013_empty_scope_closure"),
        ("cya_inspections", "0005_auto_close_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkrecord",
            name="verification_for_deficiency",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_deficiencies.deficiency",
            ),
        ),
        migrations.AddField(
            model_name="checkrecord",
            name="verification_scope_revision",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("purpose", "ROUTINE"),
                        ("verification_for_deficiency__isnull", True),
                        ("verification_scope_revision__isnull", True),
                    ),
                    models.Q(
                        ("purpose", "VERIFICATION"),
                        ("verification_for_deficiency__isnull", False),
                        ("verification_scope_revision__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="check_verification_pairing",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_data),
    ]
