"""Stable keys for vehicles and checklist templates (SPEC 2.1.0 IMP-6, issue #129).

vehicle.key is the VIN and checklist_template.key the template key. Each is null until set and
then never changed: a schema-owner trigger refuses any update that changes a key once it is set
(set once, then immutable). Uniqueness per organization and the formats are checks.
"""

from django.db import migrations, models


def set_once(table):
    return (
        f"CREATE TRIGGER cya_{table}_key_bu BEFORE UPDATE ON `{table}` FOR EACH ROW BEGIN "
        "IF OLD.`key` IS NOT NULL AND NOT (BINARY OLD.`key` <=> BINARY NEW.`key`) THEN "
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a key is set once and never changed'; "
        "END IF; END"
    )


TABLES = ("vehicle", "checklist_template")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0004_compartment_sealable"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklisttemplate",
            name="key",
            field=models.CharField(blank=True, db_collation="ascii_bin", max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="vehicle",
            name="key",
            field=models.CharField(blank=True, db_collation="ascii_bin", max_length=17, null=True),
        ),
        migrations.AddConstraint(
            model_name="checklisttemplate",
            constraint=models.UniqueConstraint(
                fields=("organization", "key"), name="template_org_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="checklisttemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("key__isnull", True), ("key__regex", "^[a-z0-9][a-z0-9._-]*$"), _connector="OR"
                ),
                name="template_key_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="vehicle",
            constraint=models.UniqueConstraint(
                fields=("organization", "key"), name="vehicle_org_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="vehicle",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("key__isnull", True), ("key__regex", "^[A-HJ-NPR-Z0-9]{17}$"), _connector="OR"
                ),
                name="vehicle_key_vin",
            ),
        ),
        migrations.RunSQL(
            [set_once(table) for table in TABLES],
            reverse_sql=[f"DROP TRIGGER cya_{table}_key_bu" for table in TABLES],
        ),
    ]
