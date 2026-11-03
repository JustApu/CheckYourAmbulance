"""Index obligations and selections by vehicle and due instant for window reads (issue #63).

Both window reads filter on vehicle and a due interval; without these they scanned every
obligation and selection the vehicle ever had. No foreign key's implicit index has these
columns as a prefix, so InnoDB keeps every existing index (see cya_checks 0011).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0006_ad_hoc_obligations")]

    operations = [
        migrations.AddIndex(
            model_name="obligation",
            index=models.Index(
                fields=["organization", "vehicle", "due_at"], name="obligation_vehicle_due"
            ),
        ),
        migrations.AddIndex(
            model_name="obligationselection",
            index=models.Index(
                fields=["organization", "vehicle", "due_at"], name="selection_vehicle_due"
            ),
        ),
    ]
