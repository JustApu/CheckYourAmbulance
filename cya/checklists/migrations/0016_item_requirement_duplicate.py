"""Item requirements copied with a duplicated vehicle (SPEC 6.0.0 CHK-11, CFG-4; issue #256 C4).

duplicate_vehicle copies the source's item requirements to the new, inactive vehicle under its
VEHICLE_DUPLICATE receipt, so the requirement and version insert guards (migrations 0014 and
0015) accept that type too. Every other check of the guards is unchanged.
"""

from importlib import import_module

from django.db import migrations

guards = import_module("cya.checklists.migrations.0014_item_requirement_guards")
publish = import_module("cya.checklists.migrations.0015_item_requirement_publish")
REQUIREMENT = "vehicle_item_requirement"
VERSION = publish.TABLE
DUPLICATE = ",'VEHICLE_DUPLICATE'"
REQUIREMENT_TYPES = guards.PUBLISH_OR_CUTOVER + DUPLICATE
VERSION_TYPES = publish.TYPES + DUPLICATE


class Migration(migrations.Migration):
    dependencies = [("cya_checklists", "0015_item_requirement_publish")]

    operations = [
        migrations.RunSQL(
            [
                f"DROP TRIGGER cya_{REQUIREMENT}_bi",
                guards.receipt_guard(REQUIREMENT, REQUIREMENT_TYPES),
                f"DROP TRIGGER cya_{VERSION}_bi",
                guards.receipt_guard(VERSION, VERSION_TYPES, guards.VERSION_EXTRA),
            ],
            reverse_sql=[
                f"DROP TRIGGER cya_{VERSION}_bi",
                guards.receipt_guard(VERSION, publish.TYPES, guards.VERSION_EXTRA),
                f"DROP TRIGGER cya_{REQUIREMENT}_bi",
                guards.receipt_guard(REQUIREMENT, guards.PUBLISH_OR_CUTOVER),
            ],
        )
    ]
