"""Avoid acquiring mutable vehicle FK locks in the projector's final lock group.

Preserve the existing vehicle_id column and every cached value. Organization scope is
still supplied by the immutable obligation; this UUID is a disposable indexed lookup.
"""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0009_obligationprojection")]

    operations = [
        migrations.AlterField(
            model_name="obligationprojection",
            name="vehicle",
            field=models.ForeignKey(
                db_column="vehicle_id",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="cya_checklists.vehicle",
            ),
        ),
        migrations.RenameField(
            model_name="obligationprojection",
            old_name="vehicle",
            new_name="vehicle_id",
        ),
        migrations.SeparateDatabaseAndState(
            # The SQL index already uses this unchanged physical column. Only its
            # Django field name changes; avoid an unnecessary index rebuild.
            state_operations=[
                migrations.RemoveIndex(
                    model_name="obligationprojection", name="projection_vehicle_due"
                ),
                migrations.AddIndex(
                    model_name="obligationprojection",
                    index=models.Index(
                        fields=["organization", "vehicle_id", "due_at"],
                        name="projection_vehicle_due",
                    ),
                ),
            ],
        ),
        migrations.AlterField(
            model_name="obligationprojection",
            name="vehicle_id",
            field=cya.accounts.fields.IdentityIDField(db_column="vehicle_id"),
        ),
    ]
