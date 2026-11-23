"""Schema-owner guards for per-place deficiencies (SPEC 6.0.0 7.5, 8.1, VER-3, SUS-2; #256 C3).

A row outside a kit recorded after the organization's item-requirement cutover names the item
requirement version of its place (migration 0026). This migration:

- adds the organization-pair keys of those columns, each with its index first;
- makes the instance key of verification_scope_row, deficiency_scope_expansion and
  check_suspension "kit instance, else item requirement, else NO_KIT": a row with a version keys
  by its requirement id, names no kit, and its definition row maps to that requirement
  (item_requirement_map). A row without a version keeps the all-zero key, as before;
- requires a deficiency header's version to fit its item target outside a kit, and an affected
  row's version to be its definition row's requirement on the deficiency's vehicle;
- has an END of a suspension repeat its START's version;
- in the deficiency event guard (VER-4 closure and reopen), counts a verification check for a
  scope row by the same unit: its kit, else the requirement of its version, else NO_KIT. A scope
  row with the all-zero key and no version (recorded per item before the cutover, or for a
  deficiency with no place) keeps its per-item meaning: any check of the item outside a kit;
- creates deficiency_requirement_map's keys, append-only triggers and insert guard: written only
  by the ITEM_REQUIREMENT_CUTOVER command, for a loose-item deficiency with a recorded place and no
  version of its own, naming a version of a requirement of that vehicle and item.
"""

from importlib import import_module

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

base = import_module("cya.deficiencies.migrations.0002_deficiency_guards")
presence = import_module("cya.deficiencies.migrations.0021_kit_presence_scope")
seals = import_module("cya.deficiencies.migrations.0023_seal_deficiency_guards")
guards = import_module("cya.checklists.migrations.0014_item_requirement_guards")
units = import_module("cya.obligations.migrations.0019_item_requirement_unit_guard")

ORG = "organization_id"
NO_KIT = "'00000000-0000-0000-0000-000000000000'"
DEFICIENCY_VEHICLE = "(SELECT d.vehicle_id FROM deficiency d WHERE d.id=NEW.deficiency_id)"
signal = units.signal
key_index = guards.key_index
VERSIONED_TABLES = (
    "deficiency",
    "deficiency_affected_item",
    "verification_scope_row",
    "deficiency_scope_expansion",
    "check_suspension",
)
MAP = "deficiency_requirement_map"
MAP_FKS = [
    ("command_id", "command_receipt"),
    ("actor_user_id", "user"),
    ("deficiency_id", "deficiency"),
    ("vehicle_id", "vehicle"),
    ("item_id", "item"),
    ("requirement_id", "vehicle_item_requirement"),
    ("requirement_version_id", "vehicle_item_requirement_version"),
]


def requirement_of(version):
    """The item requirement id of a requirement version column (SQL)."""
    return (
        f"(SELECT rv.requirement_id FROM vehicle_item_requirement_version rv WHERE rv.id={version})"
    )


def _replace(sql, before, after, count=1):
    if sql.count(before) != count:
        raise ValueError(f"unexpected trigger text: {before[:60]}")
    return sql.replace(before, after)


def _append(sql, block):
    sql = sql.rstrip()
    if not sql.endswith("END"):
        raise ValueError("unexpected trigger terminator")
    return sql[: -len("END")] + block + "\nEND"


def place_fit(message, vehicle):
    """A row with a version: no kit, a definition row mapped to that version's requirement."""
    return f"""
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.kit_instance_id IS NOT NULL
 OR NEW.definition_id IS NULL OR NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
  WHERE v.id=NEW.item_requirement_version_id AND v.{ORG}=NEW.{ORG} AND v.vehicle_id={vehicle}
  AND v.item_id=NEW.item_id AND v.requirement_id={units.requirement_of_row("NEW.definition_id")}))
THEN {signal(message)}; END IF;"""


OLD_KEY = (
    "IF NOT (BINARY NEW.instance_key <=> BINARY "
    "IFNULL(NEW.kit_instance_id,'00000000-0000-0000-0000-000000000000'))"
)
NEW_KEY = (
    "IF NOT (BINARY NEW.instance_key <=> BINARY COALESCE(NEW.kit_instance_id,"
    f"{requirement_of('NEW.item_requirement_version_id')},{NO_KIT}))"
)


def scope_insert():
    sql = _replace(presence.scope_insert(), OLD_KEY, NEW_KEY)
    return _append(
        sql, place_fit("verification scope requirement does not fit the item", DEFICIENCY_VEHICLE)
    )


def expansion_insert():
    sql = _replace(presence.expansion_insert(), OLD_KEY, NEW_KEY)
    return _append(
        sql, place_fit("scope expansion requirement does not fit the item", DEFICIENCY_VEHICLE)
    )


END_OLD = " AND s.kit_instance_id <=> NEW.kit_instance_id)"
END_NEW = (
    " AND s.kit_instance_id <=> NEW.kit_instance_id"
    " AND s.item_requirement_version_id <=> NEW.item_requirement_version_id)"
)


def suspension_insert():
    sql = _replace(presence.suspension_insert(), OLD_KEY, NEW_KEY)
    sql = _replace(sql, END_OLD, END_NEW)
    return _append(
        sql, place_fit("check suspension requirement does not fit the item", "NEW.vehicle_id")
    )


def deficiency_insert_old():
    return seals._append_guard(
        seals._extend_commands(base.DEFICIENCY_INSERT, 1), seals.HEADER_GUARD
    )


HEADER_PLACE = f"""
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.item_id IS NULL OR NEW.kit_id IS NOT NULL
 OR NEW.compartment_id IS NOT NULL OR NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
  WHERE v.id=NEW.item_requirement_version_id AND v.{ORG}=NEW.{ORG}
  AND v.vehicle_id=NEW.vehicle_id AND v.item_id=NEW.item_id))
THEN {signal("deficiency requirement does not fit its item target")}; END IF;"""


def deficiency_insert():
    return _append(deficiency_insert_old(), HEADER_PLACE)


def affected_insert_old():
    return seals._extend_commands(base.AFFECTED_INSERT, 1)


def affected_insert():
    sql = affected_insert_old()
    block = f"""
IF NEW.item_requirement_version_id IS NOT NULL AND (NEW.definition_id IS NULL OR NOT EXISTS (
 SELECT 1 FROM vehicle_item_requirement_version v WHERE v.id=NEW.item_requirement_version_id
  AND v.{ORG}=NEW.{ORG} AND v.vehicle_id={DEFICIENCY_VEHICLE} AND v.item_id=NEW.item_id
  AND v.requirement_id={units.requirement_of_row("NEW.definition_id")}))
THEN {signal("affected item requirement does not fit the item row")}; END IF;"""
    return _append(sql, block)


CHECK_UNIT_OLD = f"BINARY IFNULL(q.kit_id,{NO_KIT})=BINARY s.instance_key"
CHECK_UNIT_NEW = (
    f"(BINARY COALESCE(q.kit_id,{requirement_of('q.item_requirement_version_id')},{NO_KIT})"
    "=BINARY s.instance_key OR (q.kit_id IS NULL AND s.item_requirement_version_id IS NULL"
    f" AND BINARY s.instance_key=BINARY {NO_KIT}))"
)


def event_insert():
    """The deficiency_event insert trigger this migration installs."""
    return _replace(seals.event_insert(), CHECK_UNIT_OLD, CHECK_UNIT_NEW, count=2)


MAP_INSERT = f"""CREATE TRIGGER cya_{MAP}_bi BEFORE INSERT ON {MAP}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id <=> NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='ITEM_REQUIREMENT_CUTOVER')
THEN {signal("deficiency requirement map receipt attribution mismatch")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency d JOIN deficiency_place p ON p.deficiency_id=d.id
 WHERE d.id=NEW.deficiency_id AND d.{ORG}=NEW.{ORG} AND d.vehicle_id=NEW.vehicle_id
 AND d.item_id=NEW.item_id AND d.kit_id IS NULL AND d.compartment_id IS NULL
 AND d.item_requirement_version_id IS NULL)
THEN {signal("a deficiency requirement map needs a placed loose-item deficiency")}; END IF;
IF NOT EXISTS (SELECT 1 FROM vehicle_item_requirement_version v
 WHERE v.id=NEW.requirement_version_id AND v.{ORG}=NEW.{ORG}
 AND v.requirement_id=NEW.requirement_id AND v.vehicle_id=NEW.vehicle_id
 AND v.item_id=NEW.item_id)
THEN {signal("a deficiency requirement map names a requirement of its vehicle and item")};
END IF;
END"""


def _column_key(table):
    return f"cya_{table}_itemreqver_org_fk"


def forward():
    statements = []
    for table in VERSIONED_TABLES:
        name = _column_key(table)
        statements += [
            key_index(table, name, f"{ORG},item_requirement_version_id"),
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` "
            f"FOREIGN KEY ({ORG},item_requirement_version_id) "
            f"REFERENCES `vehicle_item_requirement_version` ({ORG},id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT",
        ]
    statements += [
        f"ALTER TABLE `{MAP}` ADD CONSTRAINT `cya_{MAP}_uuid` CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{MAP}` ADD CONSTRAINT `cya_{MAP}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{guards.ROLE_CODES}', actor_roles_at_time))",
    ]
    for column, parent in MAP_FKS:
        name = f"cya_{MAP}_{column}_org_fk"
        statements += [
            key_index(MAP, name, f"{ORG},{column}"),
            f"ALTER TABLE `{MAP}` ADD CONSTRAINT `{name}` FOREIGN KEY ({ORG},{column}) "
            f"REFERENCES `{parent}` ({ORG},id) ON DELETE RESTRICT ON UPDATE RESTRICT",
        ]
    statements += [*append_only_triggers(MAP), MAP_INSERT]
    statements += [
        "DROP TRIGGER cya_deficiency_bi",
        deficiency_insert(),
        "DROP TRIGGER cya_deficiency_affected_item_bi",
        affected_insert(),
        "DROP TRIGGER cya_verification_scope_row_bi",
        scope_insert(),
        "DROP TRIGGER cya_deficiency_scope_expansion_bi",
        expansion_insert(),
        "DROP TRIGGER cya_check_suspension_bi",
        suspension_insert(),
        "DROP TRIGGER cya_deficiency_event_bi",
        event_insert(),
    ]
    return statements


def reverse():
    statements = [
        "DROP TRIGGER cya_deficiency_event_bi",
        seals.event_insert(),
        "DROP TRIGGER cya_check_suspension_bi",
        presence.suspension_insert(),
        "DROP TRIGGER cya_deficiency_scope_expansion_bi",
        presence.expansion_insert(),
        "DROP TRIGGER cya_verification_scope_row_bi",
        presence.scope_insert(),
        "DROP TRIGGER cya_deficiency_affected_item_bi",
        affected_insert_old(),
        "DROP TRIGGER cya_deficiency_bi",
        deficiency_insert_old(),
        f"DROP TRIGGER cya_{MAP}_bi",
        f"DROP TRIGGER cya_{MAP}_bd",
        f"DROP TRIGGER cya_{MAP}_bu",
    ]
    statements += [
        f"ALTER TABLE `{MAP}` DROP FOREIGN KEY `cya_{MAP}_{column}_org_fk`"
        for column, _parent in reversed(MAP_FKS)
    ]
    statements += [
        f"ALTER TABLE `{MAP}` DROP CONSTRAINT `cya_{MAP}_roles`",
        f"ALTER TABLE `{MAP}` DROP CONSTRAINT `cya_{MAP}_uuid`",
    ]
    statements += [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `{_column_key(table)}`"
        for table in reversed(VERSIONED_TABLES)
    ]
    return statements


def forbid_reverse_with_places(apps, schema_editor):
    models = [
        apps.get_model("cya_deficiencies", name)
        for name in (
            "Deficiency",
            "DeficiencyAffectedItem",
            "VerificationScopeRow",
            "DeficiencyScopeExpansion",
            "CheckSuspension",
        )
    ]
    Map = apps.get_model("cya_deficiencies", "DeficiencyRequirementMap")
    if Map.objects.exists() or any(
        model.objects.filter(item_requirement_version__isnull=False).exists() for model in models
    ):
        raise RuntimeError("cannot reverse per-place deficiency guards while rows name a place")


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0026_item_requirement_places"),
        ("cya_checklists", "0015_item_requirement_publish"),
        ("cya_obligations", "0019_item_requirement_unit_guard"),
    ]

    # Reversal runs the data guard first (operations reverse in the opposite order).
    operations = [
        migrations.RunSQL(forward(), reverse_sql=reverse()),
        migrations.RunPython(migrations.RunPython.noop, forbid_reverse_with_places),
    ]
