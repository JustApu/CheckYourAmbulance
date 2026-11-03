"""Index obligations by vehicle item and grace deadline for check linking (issue #61).

On-time linking reads obligations whose grace deadline is still ahead and late linking those
whose deadline passed within w_late (SPEC SCH-2, 1.26.0); both are ranges on grace_at, so
neither reads an item's settled history. The leading columns are those of the natural-key
unique index, which InnoDB keeps, so no implicit foreign-key index is replaced.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0007_window_indexes")]

    operations = [
        migrations.AddIndex(
            model_name="obligation",
            index=models.Index(
                fields=["organization", "vehicle", "item", "kit_key", "grace_at"],
                name="obligation_item_grace",
            ),
        ),
    ]
