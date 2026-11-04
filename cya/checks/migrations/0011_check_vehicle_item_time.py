"""Index check records by vehicle, item, kit and time for bounded recent reads (issue #63).

InnoDB silently drops the index it created implicitly for a foreign key as soon as another
index can serve that key, and this one serves the (organization, vehicle) key. So the forward
step drops that key's index itself (when it is still there), leaving the same schema whatever
the database's history, and the reverse step re-creates it under its original name before
dropping this index, which InnoDB would otherwise refuse (error 1553).
"""

from django.db import migrations, models

INDEX = "check_vehicle_item_time"
VEHICLE_KEY_INDEX = "cya_check_record_vehicle_id_org_fk"

FORWARD = [
    f"CREATE INDEX `{INDEX}` ON `check_record` "
    "(`organization_id`, `vehicle_id`, `item_id`, `kit_id`, `recorded_at`)",
    f"DROP INDEX IF EXISTS `{VEHICLE_KEY_INDEX}` ON `check_record`",
]
REVERSE = [
    f"ALTER TABLE `check_record` ADD INDEX `{VEHICLE_KEY_INDEX}` "
    f"(`organization_id`, `vehicle_id`), DROP INDEX `{INDEX}`",
]


class Migration(migrations.Migration):
    dependencies = [("cya_checks", "0010_verification_checks")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(FORWARD, REVERSE)],
            state_operations=[
                migrations.AddIndex(
                    model_name="checkrecord",
                    index=models.Index(
                        fields=["organization", "vehicle", "item", "kit", "recorded_at"], name=INDEX
                    ),
                ),
            ],
        ),
    ]
