"""Store each obligation projection row's facts digest (issue #99, Part B option B0b).

A STORED generated column, facts_digest = SHA2(facts, 256), computed by MariaDB when a row is
written. Current reads compare it with the per-process cache key instead of hashing every row on
each read. obligation_projection is disposable (SPEC 17.4): adding the column rebuilds the table,
whose time is measured by rehearsal on MariaDB 10.11 and 11.8 before this ships. Reversing drops
the column; no other data changes.
"""

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0013_late_window_no_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="obligationprojection",
            name="facts_digest",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.text.SHA256("facts"),
                output_field=models.CharField(max_length=64, null=True),
            ),
        ),
    ]
