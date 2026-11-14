"""Guards accept a vehicle file import's receipt (SPEC 2.1.0 IMP-3, IMP-7; issue #129).

A vehicle file import is one audited command (VEHICLE_IMPORT) that applies everything or nothing,
so the rows it writes carry its receipt:

- kit_assignment_event: the kits it adds take their first event under VEHICLE_IMPORT, as under
  KIT_ADD or KIT_ASSIGNMENT_RECORD.
- kit_instance_definition: a kit it adds gets its fixed definition (KDEF-3) only as the import
  recorded it: the receipt's response lists {"kit_id", "definition_id"} under "kits", and the row
  must match one entry. The other rules (the kit's first event is from the same command, the
  definition is active) are unchanged.
- requirement_binding: the import appends a vehicle's first binding to a template under
  VEHICLE_IMPORT, as REQUIREMENT_BIND does.

Each rewritten trigger is the previous migration's, transformed by exact-text replacement.
"""

from importlib import import_module

from django.db import migrations

kit_guards = import_module("cya.kits.migrations.0002_kit_guards")
definition_guards = import_module("cya.kits.migrations.0005_kit_definition_guards")
binding_guards = import_module("cya.obligations.migrations.0005_obligations_command_type_guards")


def _replace(text, before, after):
    assert text.count(before) == 1, (before, text.count(before))
    return text.replace(before, after)


def event_insert():
    return _replace(
        kit_guards.EVENT_INSERT,
        "r.command_type IN ('KIT_ADD','KIT_ASSIGNMENT_RECORD')",
        "r.command_type IN ('KIT_ADD','KIT_ASSIGNMENT_RECORD','VEHICLE_IMPORT')",
    )


def instance_definition_insert():
    sql = _replace(
        definition_guards.INSTANCE_DEFINITION_INSERT,
        "AND r.recorded_at=NEW.recorded_at AND r.command_type='KIT_ADD')",
        "AND r.recorded_at=NEW.recorded_at AND r.command_type IN ('KIT_ADD','VEHICLE_IMPORT'))",
    )
    return _replace(
        sql,
        "AND JSON_VALUE(r.response_body, '$.definition_id') = NEW.definition_id)",
        "AND (JSON_VALUE(r.response_body, '$.definition_id') = NEW.definition_id"
        " OR (r.command_type='VEHICLE_IMPORT' AND JSON_CONTAINS(r.response_body,"
        " JSON_OBJECT('kit_id', NEW.kit_id, 'definition_id', NEW.definition_id), '$.kits'))))",
    )


def binding_insert():
    return _replace(
        binding_guards.BINDING_INSERT_NEW,
        "AND r.recorded_at=NEW.recorded_at AND r.command_type = 'REQUIREMENT_BIND')",
        "AND r.recorded_at=NEW.recorded_at AND r.command_type IN ('REQUIREMENT_BIND','VEHICLE_IMPORT'))",
    )


TRIGGERS = [
    ("cya_kit_assignment_event_bi", event_insert, kit_guards.EVENT_INSERT),
    (
        "cya_kit_instance_definition_bi",
        instance_definition_insert,
        definition_guards.INSTANCE_DEFINITION_INSERT,
    ),
    ("cya_requirement_binding_bi", binding_insert, binding_guards.BINDING_INSERT_NEW),
]
FORWARD, REVERSE = [], []
for name, new, old in TRIGGERS:
    FORWARD += [f"DROP TRIGGER {name}", new()]
    REVERSE += [f"DROP TRIGGER {name}", old]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_kits", "0011_position_keys"),
        ("cya_obligations", "0016_kit_presence_requirements"),
    ]

    operations = [migrations.RunSQL(FORWARD, reverse_sql=REVERSE)]
