"""Give every existing organization the default severity levels (SPEC 8.3, OQ-56)."""

from django.db import migrations
from django.utils import timezone

from cya.deficiencies.severity import DEFAULT_LEVELS


def seed(apps, schema_editor):
    Organization = apps.get_model("cya_accounts", "Organization")
    Severity = apps.get_model("cya_deficiencies", "DeficiencySeverity")
    now = timezone.now()
    for organization in Organization.objects.all():
        for rank, (code, label, description) in enumerate(DEFAULT_LEVELS, start=1):
            Severity.objects.get_or_create(
                organization_id=organization.pk,
                code=code,
                revision=1,
                defaults={
                    "label": label,
                    "description": description,
                    "rank": rank,
                    "active": True,
                    "effective_at": now,
                },
            )


class Migration(migrations.Migration):
    dependencies = [("cya_deficiencies", "0002_deficiency_guards")]
    # Reversing 0002 drops the table; the frozen rows are not deleted one by one.
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
