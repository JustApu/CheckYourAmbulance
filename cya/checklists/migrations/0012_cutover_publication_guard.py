"""The publication and binding insert guards accept the 5.0.0 migration's receipt (SPEC 18.2).

The migration (cya.checklists.cutover) is one CHECKLIST_CUTOVER command that publishes each
migrated vehicle checklist version and appends its schedule assignments. The publication guard
is 0003's and the binding guard is cya_obligations 0017's, each transformed by exact-text
replacement so nothing else about them changes.
"""

from importlib import import_module

from django.db import migrations

publications = import_module("cya.checklists.migrations.0003_publication_command_type_guards")
bindings = import_module("cya.obligations.migrations.0017_vehicle_checklist_assignment_guard")

PUBLICATION_BEFORE = "r.command_type IN ('CHECKLIST_PUBLISH', 'OVERRIDE_PUBLISH')"
PUBLICATION_AFTER = (
    "r.command_type IN ('CHECKLIST_PUBLISH', 'OVERRIDE_PUBLISH', 'CHECKLIST_CUTOVER')"
)
BINDING_BEFORE = "r.command_type IN ('REQUIREMENT_BIND','VEHICLE_IMPORT','VEHICLE_DUPLICATE'))"
BINDING_AFTER = (
    "r.command_type IN ('REQUIREMENT_BIND','VEHICLE_IMPORT','VEHICLE_DUPLICATE',"
    "'CHECKLIST_CUTOVER'))"
)


def _replace(sql, before, after):
    assert sql.count(before) == 1, sql.count(before)
    return sql.replace(before, after)


def publication_insert():
    return _replace(publications.PUBLICATION_INSERT_NEW, PUBLICATION_BEFORE, PUBLICATION_AFTER)


def binding_insert():
    return _replace(bindings.binding_insert(), BINDING_BEFORE, BINDING_AFTER)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0011_legacy_map_guards"),
        ("cya_obligations", "0017_vehicle_checklist_assignment_guard"),
    ]

    operations = [
        migrations.RunSQL(
            [
                "DROP TRIGGER cya_checklist_publication_bi",
                publication_insert(),
                "DROP TRIGGER cya_requirement_binding_bi",
                binding_insert(),
            ],
            reverse_sql=[
                "DROP TRIGGER cya_requirement_binding_bi",
                bindings.binding_insert(),
                "DROP TRIGGER cya_checklist_publication_bi",
                publications.PUBLICATION_INSERT_NEW,
            ],
        )
    ]
