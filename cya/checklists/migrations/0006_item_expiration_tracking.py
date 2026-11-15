"""Retain expiration tracking on each immutable checklist item definition."""

from django.db import migrations, models


class Migration(migrations.Migration):
    # Rebase onto issue #129's 0005_stable_keys once that migration is merged.
    dependencies = [("cya_checklists", "0005_stable_keys")]

    operations = [
        migrations.AddField(
            model_name="checklistitemdef",
            name="expiration_tracked",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="checklistitemdef",
            constraint=models.CheckConstraint(
                condition=models.Q(expiration_tracked__in=[True, False]),
                name="itemdef_expiration_bool",
            ),
        ),
    ]
