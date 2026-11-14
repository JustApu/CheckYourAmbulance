"""Each kit definition gets its presence item (SPEC 2.0.0 4.6 KDEF-12, issue #129).

The column is added nullable, every existing definition gets a new item named after it
("<label> (present)"), and a CHECK then requires the column. Django's state says NOT NULL; the
database keeps the column nullable under that CHECK, because Django's MODIFY would drop the
column's ascii_bin collation. The reverse leaves the created items in place: items are mutable
reference data and harmless when unused.
"""

import django.db.models.deletion
from django.db import migrations, models

from cya.accounts.fields import new_id

SUFFIX = " (present)"  # cya.kits.presence.SUFFIX, frozen here


def create_presence_items(apps, schema_editor):
    KitDefinition = apps.get_model("cya_kits", "KitDefinition")
    Item = apps.get_model("cya_checklists", "Item")
    for definition in KitDefinition.objects.filter(presence_item__isnull=True).order_by("pk"):
        item = Item.objects.create(
            id=new_id(),
            organization_id=definition.organization_id,
            label=f"{definition.label}{SUFFIX}",
            active=True,
        )
        KitDefinition.objects.filter(pk=definition.pk).update(presence_item=item)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0004_compartment_sealable"),
        ("cya_kits", "0008_kit_definition_item_id_item"),
    ]

    operations = [
        migrations.AddField(
            model_name="kitdefinition",
            name="presence_item",
            field=models.OneToOneField(
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.item",
            ),
        ),
        migrations.RunPython(create_presence_items, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="kitdefinition",
                    name="presence_item",
                    field=models.OneToOneField(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.item",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE `kit_definition` ADD CONSTRAINT "
                    "`cya_kit_definition_presence_item_set` CHECK (presence_item_id IS NOT NULL)",
                    reverse_sql="ALTER TABLE `kit_definition` DROP CONSTRAINT "
                    "`cya_kit_definition_presence_item_set`",
                ),
            ],
        ),
    ]
