"""More commands may write a vehicle position key (SPEC 5.0.0 CFG-2, CFG-4, IMP-6; issue #220).

After the cutover a new position's key belongs to its vehicle: adding a position
(POSITION_ADD), setting a missing key (POSITION_KEY_SET), duplicating a vehicle
(VEHICLE_DUPLICATE) and importing a vehicle file (VEHICLE_IMPORT) write one. The guard is 0016's
with those command types added; nothing else about it changes.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.kits.migrations.0016_vehicle_position_key_guards")

TYPES = (
    "CHECKLIST_CUTOVER",
    "POSITION_ADD",
    "POSITION_KEY_SET",
    "VEHICLE_DUPLICATE",
    "VEHICLE_IMPORT",
)
NAME = "cya_vehicle_position_key_bi"


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0016_vehicle_position_key_guards")]

    operations = [
        migrations.RunSQL(
            [f"DROP TRIGGER {NAME}", previous.insert_guard(TYPES)],
            reverse_sql=[f"DROP TRIGGER {NAME}", previous.insert_guard()],
        )
    ]
