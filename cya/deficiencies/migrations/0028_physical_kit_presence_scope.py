"""Allow verification scope rows for the presence of one assigned physical kit."""

from importlib import import_module

from django.db import migrations, models

guards = import_module("cya.deficiencies.migrations.0027_item_requirement_place_guards")

ORG = "organization_id"


def physical_presence_fit(message, vehicle, *, skip_for_end=False):
    end_guard = " AND NEW.action<>'END'" if skip_for_end else ""
    return f"""
IF NEW.kit_instance_id IS NOT NULL AND NEW.definition_id IS NULL
 AND NEW.kit_definition_item_id IS NULL AND NEW.kit_requirement_id IS NULL{end_guard}
 AND NOT EXISTS (
  SELECT 1 FROM kit_instance k
  JOIN kit_instance_definition kd ON kd.organization_id=k.organization_id AND kd.kit_id=k.id
  JOIN kit_definition d ON d.organization_id=kd.organization_id AND d.id=kd.definition_id
  WHERE k.id=NEW.kit_instance_id AND k.{ORG}=NEW.{ORG} AND k.vehicle_id={vehicle}
   AND k.active=1 AND d.presence_item_id=NEW.item_id
 )
THEN {guards.signal(message)}; END IF;"""


def _append(sql, block):
    return guards._append(sql, block)


def scope_insert():
    return _append(
        guards.scope_insert(),
        physical_presence_fit(
            "verification presence kit does not fit the item", guards.DEFICIENCY_VEHICLE
        ),
    )


def expansion_insert():
    return _append(
        guards.expansion_insert(),
        physical_presence_fit(
            "scope expansion presence kit does not fit the item", guards.DEFICIENCY_VEHICLE
        ),
    )


def suspension_insert():
    return _append(
        guards.suspension_insert(),
        physical_presence_fit(
            "suspension presence kit does not fit the item",
            "NEW.vehicle_id",
            skip_for_end=True,
        ),
    )


TRIGGERS = (
    ("verification_scope_row", scope_insert, guards.scope_insert),
    ("deficiency_scope_expansion", expansion_insert, guards.expansion_insert),
    ("check_suspension", suspension_insert, guards.suspension_insert),
)

FORWARD = []
REVERSE = []
for table, new, old in TRIGGERS:
    FORWARD.extend([f"DROP TRIGGER cya_{table}_bi", new()])
    REVERSE.extend([f"DROP TRIGGER cya_{table}_bi", old()])


SOURCE_CONSTRAINTS = (
    (
        "checksuspension",
        "checksusp_content_source",
    ),
    ("deficiencyscopeexpansion", "defexp_content_source"),
    ("verificationscoperow", "verscope_content_source"),
)


def source_constraint(name):
    return models.CheckConstraint(
        condition=(
            models.Q(
                definition__isnull=False,
                kit_definition_item__isnull=True,
                kit_requirement__isnull=True,
            )
            | models.Q(
                definition__isnull=True,
                kit_definition_item__isnull=False,
                kit_requirement__isnull=True,
            )
            | models.Q(
                definition__isnull=True,
                kit_definition_item__isnull=True,
                kit_requirement__isnull=False,
            )
            | models.Q(
                definition__isnull=True,
                kit_definition_item__isnull=True,
                kit_requirement__isnull=True,
                kit_instance__isnull=False,
            )
        ),
        name=name,
    )


def forbid_reverse_with_physical_presence(apps, schema_editor):
    for model_name in (
        "CheckSuspension",
        "DeficiencyScopeExpansion",
        "VerificationScopeRow",
    ):
        model = apps.get_model("cya_deficiencies", model_name)
        if model.objects.filter(
            kit_instance__isnull=False,
            definition__isnull=True,
            kit_definition_item__isnull=True,
            kit_requirement__isnull=True,
        ).exists():
            raise RuntimeError("cannot reverse physical kit presence scope with saved rows")


MIGRATION_OPERATIONS = []
for model_name, constraint_name in SOURCE_CONSTRAINTS:
    MIGRATION_OPERATIONS.append(
        migrations.RemoveConstraint(model_name=model_name, name=constraint_name)
    )
for model_name, constraint_name in SOURCE_CONSTRAINTS:
    MIGRATION_OPERATIONS.append(
        migrations.AddConstraint(
            model_name=model_name,
            constraint=source_constraint(constraint_name),
        )
    )
MIGRATION_OPERATIONS.extend(
    [
        migrations.RunSQL(FORWARD, reverse_sql=REVERSE),
        migrations.RunPython(migrations.RunPython.noop, forbid_reverse_with_physical_presence),
    ]
)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0027_item_requirement_place_guards"),
        ("cya_kits", "0018_active_unassigned_kits"),
    ]

    operations = MIGRATION_OPERATIONS
