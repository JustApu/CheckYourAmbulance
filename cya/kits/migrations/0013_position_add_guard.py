"""The position_key insert guard also accepts POSITION_ADD (SPEC 3.0.0 IMP-6, CFG-2; issue #130).

Whoever creates a new position enters its key (SPEC 3.0.0), so add_position writes the key row
under its own POSITION_ADD receipt. The rest of the guard (same actor, recorded instant and
organization, and a slot that exists) is unchanged; the trigger is rewritten by exact-text
replacement of 0011's.
"""

from importlib import import_module

from django.db import migrations

position_keys = import_module("cya.kits.migrations.0011_position_keys")

BEFORE = "r.command_type IN ('POSITION_KEY_SET','VEHICLE_IMPORT')"
AFTER = "r.command_type IN ('POSITION_KEY_SET','VEHICLE_IMPORT','POSITION_ADD')"
assert position_keys.INSERT_GUARD.count(BEFORE) == 1
NEW_GUARD = position_keys.INSERT_GUARD.replace(BEFORE, AFTER)


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0012_vehicle_import_guards")]

    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_position_key_bi", NEW_GUARD],
            reverse_sql=["DROP TRIGGER cya_position_key_bi", position_keys.INSERT_GUARD],
        )
    ]
