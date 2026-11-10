"""Pin w_late on every inspection and odometer obligation and store its late limit (issue #80).

SCH-2/SCH-3 (amendment 1.31.0): the late window is captured when an obligation is created and
its late limit L = G + w_late never moves. Both tables are append-only (triggers refuse UPDATE,
migration 0002), so existing rows receive the documented 12-hour default through a temporary
database default rather than an UPDATE. Migration 0013 removes that default in the same release,
so a creator that omits the value fails instead of silently taking 12 hours.

late_limit_at is a stored generated column, so existing rows get their limit when the column is
added and new rows can never disagree with their pinned window. Adding a stored generated column
rebuilds the table; the lock and build time on MariaDB 10.11 and 11.8 are measured by the
owner-approved rehearsal before this ships, never assumed. Reversing drops the new columns, index
and checks; no other data changes.
"""

from django.db import migrations, models

import cya.obligations.models

LIMIT = cya.obligations.models.TimestampAddSeconds(models.F("late_seconds"), models.F("grace_at"))


def late_limit():
    return models.GeneratedField(
        db_persist=True, expression=LIMIT, output_field=models.DateTimeField()
    )


class Migration(migrations.Migration):
    dependencies = [("cya_obligations", "0011_routine_scope_indexes")]

    operations = [
        migrations.AddField(
            model_name="obligation",
            name="late_seconds",
            field=models.PositiveIntegerField(db_default=43200),
        ),
        migrations.AddField(
            model_name="odometerobligation",
            name="late_seconds",
            field=models.PositiveIntegerField(db_default=43200),
        ),
        migrations.AddField(model_name="obligation", name="late_limit_at", field=late_limit()),
        migrations.AddField(
            model_name="odometerobligation", name="late_limit_at", field=late_limit()
        ),
        migrations.AddConstraint(
            model_name="obligation",
            constraint=models.CheckConstraint(
                condition=models.Q(late_seconds__lte=604800), name="obligation_late_seconds"
            ),
        ),
        migrations.AddConstraint(
            model_name="odometerobligation",
            constraint=models.CheckConstraint(
                condition=models.Q(late_seconds__lte=604800), name="odometer_ob_late_seconds"
            ),
        ),
        migrations.AddIndex(
            model_name="obligation",
            index=models.Index(
                fields=["organization", "vehicle", "item", "kit_key", "late_limit_at"],
                name="obligation_item_late_limit",
            ),
        ),
    ]
