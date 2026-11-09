"""Index obligations and projections by vehicle and routine flag for the readers' scope (issue #74).

The current readers select "due window OR routine = false". With only the due-instant indexes
the optimizer scans a vehicle's rows for the second arm; an (organization, vehicle, routine)
index lets it read the ad hoc rows by range and merge them with the window. Both statements are
plain index builds that InnoDB can run in place without blocking writers, and the migration is
reversible (the indexes are dropped; no data changes).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0010_projection_vehicle_lookup")]

    operations = [
        migrations.AddIndex(
            model_name="obligation",
            index=models.Index(
                fields=["organization", "vehicle", "routine"], name="obligation_vehicle_routine"
            ),
        ),
        migrations.AddIndex(
            model_name="obligationprojection",
            index=models.Index(
                fields=["organization", "vehicle_id", "routine"],
                name="projection_vehicle_routine",
            ),
        ),
    ]
