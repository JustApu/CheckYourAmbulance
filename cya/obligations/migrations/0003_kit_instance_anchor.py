"""Add the kit instance to the obligation anchor (SPEC 1.8.0 / 1.10.0, OQ-53, issue 92 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)).

An obligation, its requirement version and its selection stream gain a kit instance, so an item
inside a kit has one anchor per instance. kit_key is NOT NULL (the instance id, or all zeros for
an item outside a kit) because MariaDB UNIQUE treats NULLs as distinct, so the unique keys and
the composite anchor foreign keys must compare a value. Existing rows are all outside a kit and
take the all-zero key from the column default; no UPDATE runs, so the append-only
triggers never fire.

The composite anchor foreign keys and the two insert triggers pin the old keys, so they are
dropped first and recreated last against the new ones. Reversing fails closed: the old unique
keys cannot be restored while any row names a kit instance.
"""

from importlib import import_module

import django.db.models.deletion
from django.db import connection, migrations, models

import cya.accounts.fields

old = import_module("cya.obligations.migrations.0002_obligation_guards")

ORG = "organization_id"
NO_KIT = "00000000-0000-0000-0000-000000000000"
OLD_ANCHOR_FKS = [
    fk for fk in old.ANCHOR_FKS if fk[0] in ("version_anchor", "selected_anchor", "previous_anchor")
]
NEW_SUFFIX = "_kit"
NEW_ANCHOR_FKS = [
    (
        "version_anchor",
        "obligation",
        "requirement_version_id, vehicle_id, item_id, kit_key",
        "requirement_version",
        "id, vehicle_id, item_id, kit_key",
    ),
    (
        "selected_anchor",
        "obligation_selection",
        "selected_obligation_id, vehicle_id, item_id, kit_key, due_at",
        "obligation",
        "id, vehicle_id, item_id, kit_key, due_at",
    ),
    (
        "previous_anchor",
        "obligation_selection",
        "previous_selection_id, vehicle_id, item_id, kit_key, due_at",
        "obligation_selection",
        "id, vehicle_id, item_id, kit_key, due_at",
    ),
]
# (table, column, parent table): organization-pair keys for the new references.
KIT_FKS = [
    ("requirement_version", "kit_instance_id", "kit_instance"),
    ("requirement_version", "kit_event_id", "kit_assignment_event"),
    ("obligation", "kit_instance_id", "kit_instance"),
    ("obligation_selection", "kit_instance_id", "kit_instance"),
]


def fk_name(index):
    return f"cya_obl_kit_fk_{index:02d}"


def anchor_fk_sql(fks, suffix=""):
    return [
        f"ALTER TABLE `{table}` ADD CONSTRAINT `cya_obl_{name}{suffix}_fk` "
        f"FOREIGN KEY ({columns}) REFERENCES `{parent}` ({parent_columns}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for name, table, columns, parent, parent_columns in fks
    ]


def drop_anchor_fk_sql(fks, suffix=""):
    """Drop each anchor foreign key (its index is handled separately, see the bridges below)."""
    return [
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `cya_obl_{name}{suffix}_fk`"
        for name, table, *_ in fks
    ]


signal = old.signal

# The requirement version's kit pins must belong together (in addition to the 0002 checks): an
# item that lives in a kit names a kit instance, one outside a kit names none, and the instance
# is active on this vehicle, in the compartment slot the item's kit reference stands for, from
# the pinned event onward. The reference kit's slot is that of its first placement, as
# cya.kits.history.slot_of_reference_kit reads it, so moving or deactivating it changes nothing.
KIT_VERSION_CHECKS = f"""IF NEW.kit_instance_id IS NULL THEN
 IF EXISTS (SELECT 1 FROM checklist_item_def d WHERE d.id=NEW.item_def_id AND d.kit_id IS NOT NULL)
 THEN {signal("requirement for a kit item must name a kit instance")}; END IF;
ELSE
 IF NOT EXISTS (SELECT 1 FROM kit_assignment_event e
  JOIN compartment ec ON ec.id=e.compartment_id
  JOIN checklist_item_def d ON d.id=NEW.item_def_id
  JOIN kit_instance rk ON rk.id=d.kit_id
  LEFT JOIN kit_assignment_event rf ON rf.kit_id=rk.id AND rf.sequence=1
  JOIN compartment rc ON rc.id=IF(rf.id IS NOT NULL, rf.compartment_id, rk.compartment_id)
  WHERE e.id=NEW.kit_event_id AND e.{ORG}=NEW.{ORG} AND e.kit_id=NEW.kit_instance_id
  AND e.vehicle_id=NEW.vehicle_id AND e.active=1 AND ec.stable_slot_id=rc.stable_slot_id)
 THEN {signal("requirement kit instance is not active on the vehicle in the item slot")}; END IF;
END IF;
"""

VERSION_INSERT = old.VERSION_INSERT.rstrip()
assert VERSION_INSERT.endswith("END")
VERSION_INSERT = VERSION_INSERT[: -len("END")] + KIT_VERSION_CHECKS + "END"

# The same stream rules as 0002, with the kit key part of the anchor wherever the anchor is compared.
SELECTION_INSERT = old.SELECTION_INSERT
for fragment in (
    "AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at AND s.sequence=NEW.sequence-1)",
    "AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at\n",
):
    assert SELECTION_INSERT.count(fragment) == 1, fragment
SELECTION_INSERT = SELECTION_INSERT.replace(
    "AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at AND s.sequence=NEW.sequence-1)",
    "AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key AND s.due_at=NEW.due_at\n  AND s.sequence=NEW.sequence-1)",
).replace(
    "AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.due_at=NEW.due_at\n",
    "AND s.vehicle_id=NEW.vehicle_id AND s.item_id=NEW.item_id AND s.kit_key=NEW.kit_key\n  AND s.due_at=NEW.due_at\n",
)

# MariaDB refuses to drop an index a foreign key needs, and it silently drops a shorter
# ("redundant") index when a longer one covering it is created. So the unique keys being
# replaced, and the indexes that the anchor foreign keys leave behind, are often the only index a
# plain foreign key has: (organization_id, vehicle_id) for the organization-pair keys, and the
# leading column of each anchor key for the single-column keys Django created. Temporary
# bridge indexes carry those foreign keys while the real ones are swapped, in both directions.
BRIDGES = [
    ("cya_tmp_obligation_org_vehicle", "obligation", "organization_id, vehicle_id"),
    ("cya_tmp_selection_org_vehicle", "obligation_selection", "organization_id, vehicle_id"),
    ("cya_tmp_obligation_version", "obligation", "requirement_version_id"),
    ("cya_tmp_selection_selected", "obligation_selection", "selected_obligation_id"),
    ("cya_tmp_selection_previous", "obligation_selection", "previous_selection_id"),
]
BRIDGE_CREATE = [f"CREATE INDEX `{name}` ON `{table}` ({cols})" for name, table, cols in BRIDGES]
# IF EXISTS: MariaDB may already have dropped a bridge as redundant once a longer index exists.
BRIDGE_DROP = [f"DROP INDEX IF EXISTS `{name}` ON `{table}`" for name, table, _ in BRIDGES]


def drop_fk_indexes_sql(fks, suffix=""):
    """Drop the index MariaDB created for each anchor key (safe while the bridges exist)."""
    return [
        f"DROP INDEX IF EXISTS `cya_obl_{name}{suffix}_fk` ON `{table}`" for name, table, *_ in fks
    ]


PRE_FORWARD = [
    *BRIDGE_CREATE,
    *drop_anchor_fk_sql(OLD_ANCHOR_FKS),
    # The old keys' indexes, and any stale index of the new names a previous reversal left behind.
    *drop_fk_indexes_sql(OLD_ANCHOR_FKS),
    *drop_fk_indexes_sql(NEW_ANCHOR_FKS, NEW_SUFFIX),
    "DROP TRIGGER cya_requirement_version_bi",
    "DROP TRIGGER cya_obligation_selection_bi",
]
PRE_REVERSE = [
    *anchor_fk_sql(OLD_ANCHOR_FKS),
    old.VERSION_INSERT,
    old.SELECTION_INSERT,
    *BRIDGE_DROP,
]
POST_FORWARD = [
    *[
        f"ALTER TABLE `{table}` ADD CONSTRAINT `{fk_name(index)}` "
        f"FOREIGN KEY ({ORG},{column}) REFERENCES `{parent}` ({ORG},id) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
        for index, (table, column, parent) in enumerate(KIT_FKS, start=1)
    ],
    *anchor_fk_sql(NEW_ANCHOR_FKS, NEW_SUFFIX),
    *BRIDGE_DROP,
    VERSION_INSERT,
    SELECTION_INSERT,
]
POST_REVERSE = [
    *BRIDGE_CREATE,
    "DROP TRIGGER cya_obligation_selection_bi",
    "DROP TRIGGER cya_requirement_version_bi",
    *drop_anchor_fk_sql(NEW_ANCHOR_FKS, NEW_SUFFIX),
    *drop_fk_indexes_sql(NEW_ANCHOR_FKS, NEW_SUFFIX),
    *[
        f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk_name(index)}`"
        for index, (table, _, _) in reversed(list(enumerate(KIT_FKS, start=1)))
    ],
]


def refuse_reverse_with_kit_rows(apps, schema_editor):
    """Reversing drops the kit columns, which would silently merge per-instance obligations.

    DDL in MariaDB is not transactional, so a reverse that failed halfway would leave the schema
    half undone; check first and change nothing if any row names a kit instance.
    """
    with connection.cursor() as cursor:
        for table in ("obligation", "obligation_selection", "requirement_version"):
            cursor.execute(f"SELECT COUNT(*) FROM `{table}` WHERE kit_key <> %s", [NO_KIT])
            if cursor.fetchone()[0]:
                raise RuntimeError(
                    f"cannot reverse: {table} has rows for kit instances (SPEC 1.8.0); "
                    "nothing was changed"
                )


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0002_publication_guards"),
        ("cya_conditions", "0002_conditions_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_kits", "0002_kit_guards"),
        ("cya_obligations", "0002_obligation_guards"),
        ("cya_schedules", "0002_schedule_guards"),
    ]

    operations = [
        migrations.RunSQL(PRE_FORWARD, reverse_sql=PRE_REVERSE),
        migrations.RemoveConstraint(
            model_name="obligation",
            name="obligation_natural_key",
        ),
        migrations.RemoveConstraint(
            model_name="obligation",
            name="obligation_anchor",
        ),
        migrations.RemoveConstraint(
            model_name="obligationselection",
            name="selection_anchor_sequence",
        ),
        migrations.RemoveConstraint(
            model_name="obligationselection",
            name="selection_anchor_id",
        ),
        migrations.RemoveConstraint(
            model_name="requirementversion",
            name="reqversion_anchor",
        ),
        migrations.AddField(
            model_name="obligation",
            name="kit_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.kitinstance",
            ),
        ),
        migrations.AddField(
            model_name="obligation",
            name="kit_key",
            field=cya.accounts.fields.IdentityIDField(
                db_collation="ascii_bin",
                default="00000000-0000-0000-0000-000000000000",
                max_length=36,
            ),
        ),
        migrations.AddField(
            model_name="obligationselection",
            name="kit_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.kitinstance",
            ),
        ),
        migrations.AddField(
            model_name="obligationselection",
            name="kit_key",
            field=cya.accounts.fields.IdentityIDField(
                db_collation="ascii_bin",
                default="00000000-0000-0000-0000-000000000000",
                max_length=36,
            ),
        ),
        migrations.AddField(
            model_name="requirementversion",
            name="kit_event",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_kits.kitassignmentevent",
            ),
        ),
        migrations.AddField(
            model_name="requirementversion",
            name="kit_instance",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.kitinstance",
            ),
        ),
        migrations.AddField(
            model_name="requirementversion",
            name="kit_key",
            field=cya.accounts.fields.IdentityIDField(
                db_collation="ascii_bin",
                default="00000000-0000-0000-0000-000000000000",
                max_length=36,
            ),
        ),
        migrations.AddConstraint(
            model_name="obligation",
            constraint=models.UniqueConstraint(
                fields=(
                    "organization",
                    "vehicle",
                    "item",
                    "kit_key",
                    "due_at",
                    "requirement_version",
                ),
                name="obligation_natural_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="obligation",
            constraint=models.UniqueConstraint(
                fields=("id", "vehicle", "item", "kit_key", "due_at"), name="obligation_anchor"
            ),
        ),
        migrations.AddConstraint(
            model_name="obligation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kit_instance__isnull", True),
                        ("kit_key", "00000000-0000-0000-0000-000000000000"),
                    ),
                    models.Q(
                        ("kit_instance", models.F("kit_key")),
                        ("kit_instance__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="obligation_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="obligationselection",
            constraint=models.UniqueConstraint(
                fields=("organization", "vehicle", "item", "kit_key", "due_at", "sequence"),
                name="selection_anchor_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="obligationselection",
            constraint=models.UniqueConstraint(
                fields=("id", "vehicle", "item", "kit_key", "due_at"), name="selection_anchor_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="obligationselection",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kit_instance__isnull", True),
                        ("kit_key", "00000000-0000-0000-0000-000000000000"),
                    ),
                    models.Q(
                        ("kit_instance", models.F("kit_key")),
                        ("kit_instance__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="selection_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.UniqueConstraint(
                fields=("id", "vehicle", "item", "kit_key"), name="reqversion_anchor"
            ),
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kit_instance__isnull", True),
                        ("kit_key", "00000000-0000-0000-0000-000000000000"),
                    ),
                    models.Q(
                        ("kit_instance", models.F("kit_key")),
                        ("kit_instance__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="reqversion_kit_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="requirementversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kit_event__isnull", True), ("kit_instance__isnull", True)),
                    models.Q(("kit_event__isnull", False), ("kit_instance__isnull", False)),
                    _connector="OR",
                ),
                name="reqversion_kit_event",
            ),
        ),
        migrations.RunSQL(POST_FORWARD, reverse_sql=POST_REVERSE),
        migrations.RunPython(migrations.RunPython.noop, reverse_code=refuse_reverse_with_kit_rows),
    ]
