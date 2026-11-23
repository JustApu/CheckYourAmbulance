"""Item requirement versions published on their own (SPEC 6.0.0 CHK-11, AC-42 (4); issue #256 C2).

publish_item_requirement_version writes a requirement version under an ITEM_REQUIREMENT_PUBLISH
receipt; the version table's insert guard (migration 0014) accepts that type too.
"""

from importlib import import_module

from django.db import migrations

guards = import_module("cya.checklists.migrations.0014_item_requirement_guards")
TABLE = "vehicle_item_requirement_version"
TYPES = guards.PUBLISH_OR_CUTOVER + ",'ITEM_REQUIREMENT_PUBLISH'"


class Migration(migrations.Migration):
    dependencies = [("cya_checklists", "0014_item_requirement_guards")]

    operations = [
        migrations.RunSQL(
            [
                f"DROP TRIGGER cya_{TABLE}_bi",
                guards.receipt_guard(TABLE, TYPES, guards.VERSION_EXTRA),
            ],
            reverse_sql=[
                f"DROP TRIGGER cya_{TABLE}_bi",
                guards.receipt_guard(TABLE, guards.PUBLISH_OR_CUTOVER, guards.VERSION_EXTRA),
            ],
        )
    ]
