"""Allow an active physical kit to be tracked as unassigned stock (issue #273).

An active event with no vehicle or compartment means the kit is in stock and has not been assigned.
An inactive event still means the kit has been retired from tracked inventory. The event remains
the source of truth; kit_instance is its current projection.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0017_vehicle_position_key_writers")]

    operations = [
        migrations.RemoveConstraint(
            model_name="kitassignmentevent",
            name="kitassign_placement",
        ),
        migrations.AddConstraint(
            model_name="kitassignmentevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(active=True, vehicle__isnull=True, compartment__isnull=True)
                    | models.Q(active=True, vehicle__isnull=False, compartment__isnull=False)
                    | models.Q(active=False, vehicle__isnull=True, compartment__isnull=True)
                ),
                name="kitassign_placement",
            ),
        ),
    ]
