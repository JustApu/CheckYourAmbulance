"""Remove the temporary 12-hour database default for the pinned late window (issue #80).

Migration 0012 used the default only to give existing rows the documented 12 hours without an
UPDATE. Every creator now supplies the captured setting explicitly, so from here an omitted value
is an error rather than a silent 12 hours. Reversing restores the default.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0012_pin_late_window")]

    operations = [
        migrations.AlterField(
            model_name="obligation",
            name="late_seconds",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="odometerobligation",
            name="late_seconds",
            field=models.PositiveIntegerField(),
        ),
    ]
