"""The binding insert guard accepts a vehicle duplication's receipt (SPEC 4.8 CFG-4, issue #130).

Duplicating a vehicle is one audited command (VEHICLE_DUPLICATE) that, among other things,
appends the copy's first binding to each template the source is bound to, as REQUIREMENT_BIND
and VEHICLE_IMPORT do. The trigger is 0012's, transformed by exact-text replacement.
"""

from importlib import import_module

from django.db import migrations

previous = import_module("cya.kits.migrations.0012_vehicle_import_guards")

BEFORE = "r.command_type IN ('REQUIREMENT_BIND','VEHICLE_IMPORT'))"
AFTER = "r.command_type IN ('REQUIREMENT_BIND','VEHICLE_IMPORT','VEHICLE_DUPLICATE'))"


def binding_insert():
    sql = previous.binding_insert()
    assert sql.count(BEFORE) == 1, sql.count(BEFORE)
    return sql.replace(BEFORE, AFTER)


NAME = "cya_requirement_binding_bi"


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0013_position_add_guard")]

    operations = [
        migrations.RunSQL(
            [f"DROP TRIGGER {NAME}", binding_insert()],
            reverse_sql=[f"DROP TRIGGER {NAME}", previous.binding_insert()],
        )
    ]
