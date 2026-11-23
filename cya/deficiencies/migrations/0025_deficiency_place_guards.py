"""Schema-owner guards for deficiency places (SPEC 6.0.0 18.3 item 2; issue #256 C3).

deficiency_place is append-only (A) and attributed to a DEFICIENCY_PLACE_ASSIGN receipt of the
same organization, actor and instant, from a Supervisor or Administrator. Its vehicle and item are
the deficiency's, which names an item outside a kit (kit and compartment null), and its position,
when it has one, is a compartment or system of that vehicle whose stable slot is its place. One
place per deficiency (a unique key). Whether a published checklist keeps the item there is the
command's rule: it reads published versions, which a trigger cannot replay cheaply.
"""

from importlib import import_module

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN
from tools.audit_enforcement import append_only_triggers

guards = import_module("cya.checklists.migrations.0014_item_requirement_guards")
key_index = guards.key_index
signal = guards.signal

TABLE = "deficiency_place"
ORG = "organization_id"
ROLE_CODES = guards.ROLE_CODES
FKS = [
    ("command_id", "command_receipt"),
    ("actor_user_id", "user"),
    ("deficiency_id", "deficiency"),
    ("vehicle_id", "vehicle"),
    ("item_id", "item"),
    ("compartment_id", "compartment"),
    ("system_id", "vehicle_system"),
]

INSERT_GUARD = f"""CREATE TRIGGER cya_{TABLE}_bi BEFORE INSERT ON {TABLE}
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.{ORG}=NEW.{ORG} AND r.actor_user_id=NEW.actor_user_id
 AND r.recorded_at=NEW.recorded_at AND r.command_type='DEFICIENCY_PLACE_ASSIGN')
THEN {signal("deficiency place receipt attribution mismatch")}; END IF;
IF NOT (JSON_CONTAINS(NEW.actor_roles_at_time,'"SUPERVISOR"')
 OR JSON_CONTAINS(NEW.actor_roles_at_time,'"ADMINISTRATOR"'))
THEN {signal("only a Supervisor or Administrator assigns a deficiency place")}; END IF;
IF NOT EXISTS (SELECT 1 FROM deficiency d WHERE d.id=NEW.deficiency_id
 AND d.{ORG}=NEW.{ORG} AND d.vehicle_id=NEW.vehicle_id AND d.item_id=NEW.item_id
 AND d.kit_id IS NULL AND d.compartment_id IS NULL)
THEN {signal("a deficiency place needs a deficiency on that vehicle and item outside a kit")};
END IF;
IF (NEW.compartment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM compartment c
  WHERE c.id=NEW.compartment_id AND c.vehicle_id=NEW.vehicle_id
  AND c.stable_slot_id=NEW.place_slot_id))
 OR (NEW.system_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vehicle_system s
  WHERE s.id=NEW.system_id AND s.vehicle_id=NEW.vehicle_id
  AND s.stable_slot_id=NEW.place_slot_id))
THEN {signal("a deficiency place names a position of another vehicle")}; END IF;
END"""


def forward_statements():
    return [
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_uuid` "
        f"CHECK (BINARY id REGEXP '{UUID_PATTERN}')",
        f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_roles` "
        "CHECK (JSON_TYPE(actor_roles_at_time)='ARRAY' AND JSON_LENGTH(actor_roles_at_time)<=4 "
        f"AND JSON_CONTAINS('{ROLE_CODES}', actor_roles_at_time))",
        *(
            statement
            for column, parent in FKS
            for statement in (
                key_index(TABLE, f"cya_{TABLE}_{column}_org_fk", f"{ORG},{column}"),
                f"ALTER TABLE `{TABLE}` ADD CONSTRAINT `cya_{TABLE}_{column}_org_fk` "
                f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
                "ON DELETE RESTRICT ON UPDATE RESTRICT",
            )
        ),
        *append_only_triggers(TABLE),
        INSERT_GUARD,
    ]


def reverse_statements():
    return [
        f"DROP TRIGGER cya_{TABLE}_bi",
        f"DROP TRIGGER cya_{TABLE}_bd",
        f"DROP TRIGGER cya_{TABLE}_bu",
        *(
            f"ALTER TABLE `{TABLE}` DROP FOREIGN KEY `cya_{TABLE}_{column}_org_fk`"
            for column, _parent in reversed(FKS)
        ),
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_roles`",
        f"ALTER TABLE `{TABLE}` DROP CONSTRAINT `cya_{TABLE}_uuid`",
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("cya_deficiencies", "0024_deficiency_place"),
        ("cya_checklists", "0014_item_requirement_guards"),
    ]

    operations = [migrations.RunSQL(forward_statements(), reverse_sql=reverse_statements())]
