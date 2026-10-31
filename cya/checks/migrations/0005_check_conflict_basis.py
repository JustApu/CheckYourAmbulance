"""A check may name the check it was shown and the newer one it proceeded past (issue 75 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/75)).

basis_check_id and acknowledges_check_id (SPEC 7.2, CON-3) are nullable references to earlier
checks of the same item. The command compares them with the newest check under the item lock;
the extended insert trigger repeats the part the database can know on its own, so a writer that
bypasses the command still cannot point a check at another item's check, at another kit
instance's check, or at one recorded after itself.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

prior = import_module("cya.checks.migrations.0003_check_session")
signal, ORG = prior.signal, prior.ORG


def reference_check(column, message):
    return f"""IF NEW.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM check_record b
 WHERE b.id=NEW.{column} AND b.{ORG}=NEW.{ORG} AND b.vehicle_id=NEW.vehicle_id
 AND b.item_id=NEW.item_id AND b.kit_id <=> NEW.kit_id AND b.recorded_at<=NEW.recorded_at)
THEN {signal(message)}; END IF;
"""


REFERENCE_CHECKS = reference_check(
    "basis_check_id", "check basis is not an earlier check of this item"
) + reference_check(
    "acknowledges_check_id", "acknowledged check is not an earlier check of this item"
)

CHECK_INSERT = prior.CHECK_INSERT.rstrip()
assert CHECK_INSERT.endswith("END")
CHECK_INSERT = CHECK_INSERT[: -len("END")] + REFERENCE_CHECKS + "END"

FORWARD = [
    f"ALTER TABLE `check_record` ADD CONSTRAINT `cya_check_record_{column}_org_fk` "
    f"FOREIGN KEY ({ORG},{column}) REFERENCES `check_record` ({ORG},id) "
    "ON DELETE RESTRICT ON UPDATE RESTRICT"
    for column in ("basis_check_id", "acknowledges_check_id")
] + ["DROP TRIGGER cya_check_record_bi", CHECK_INSERT]
REVERSE = [
    "DROP TRIGGER cya_check_record_bi",
    prior.CHECK_INSERT,
    "ALTER TABLE `check_record` DROP FOREIGN KEY `cya_check_record_acknowledges_check_id_org_fk`",
    "ALTER TABLE `check_record` DROP FOREIGN KEY `cya_check_record_basis_check_id_org_fk`",
    # Dump/reload makes InnoDB's implicit FK indexes explicit. Drop them before Django removes
    # the fields, or MariaDB retains organization-only indexes that collide on reapplication.
    "DROP INDEX IF EXISTS `cya_check_record_acknowledges_check_id_org_fk` ON `check_record`",
    "DROP INDEX IF EXISTS `cya_check_record_basis_check_id_org_fk` ON `check_record`",
]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_checks", "0004_obligation_links"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_inspections", "0005_auto_close_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkrecord",
            name="acknowledges_check",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checks.checkrecord",
            ),
        ),
        migrations.AddField(
            model_name="checkrecord",
            name="basis_check",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checks.checkrecord",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("basis_check", models.F("id")), _negated=True),
                    models.Q(("acknowledges_check", models.F("id")), _negated=True),
                ),
                name="check_basis_not_self",
            ),
        ),
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
    ]
