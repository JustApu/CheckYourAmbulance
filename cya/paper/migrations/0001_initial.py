"""Paper-entry batch header tables (SPEC 8.0.0 18.4; issue #9 Part B1); guards are in 0002."""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_checklists", "0016_item_requirement_duplicate"),
        ("cya_core", "0006_audit_action_command_type_guard"),
        ("cya_kits", "0018_active_unassigned_kits"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaperEntryBatch",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        db_collation="ascii_bin",
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("performed_at", models.DateTimeField()),
                ("performed_local_date", models.DateField()),
                ("performed_local_time", models.TimeField()),
                ("performed_zone", models.CharField(max_length=100)),
                ("performed_utc_offset_minutes", models.SmallIntegerField()),
                ("performed_resolution", models.CharField(max_length=20)),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "checklist_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.checklistversion",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
                (
                    "vehicle",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.vehicle",
                    ),
                ),
            ],
            options={
                "db_table": "paper_entry_batch",
            },
        ),
        migrations.CreateModel(
            name="PaperEntryKitVersion",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        db_collation="ascii_bin",
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_paper.paperentrybatch",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "kit_definition_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_kits.kitdefinitionversion",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
            ],
            options={
                "db_table": "paper_entry_kit_version",
            },
        ),
        migrations.CreateModel(
            name="PaperEntryPerformer",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                (
                    "id",
                    cya.accounts.fields.IdentityIDField(
                        db_collation="ascii_bin",
                        default=cya.accounts.fields.new_id,
                        editable=False,
                        max_length=36,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_paper.paperentrybatch",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_core.commandreceipt",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
                (
                    "performer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
            ],
            options={
                "db_table": "paper_entry_performer",
            },
        ),
        migrations.AddIndex(
            model_name="paperentrybatch",
            index=models.Index(
                fields=["organization", "vehicle", "performed_at"], name="paperbatch_vehicle_time"
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrybatch",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="paperbatch_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrybatch",
            constraint=models.CheckConstraint(
                condition=models.Q(("performed_at__lte", models.F("recorded_at"))),
                name="paperbatch_not_future",
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrybatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("performed_resolution__in", ("EXACT", "GAP_FORWARD", "AMBIGUOUS_FIRST"))
                ),
                name="paperbatch_resolution",
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrybatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("performed_utc_offset_minutes__gte", -1080),
                    ("performed_utc_offset_minutes__lte", 1080),
                ),
                name="paperbatch_offset_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrykitversion",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="paperkitver_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentrykitversion",
            constraint=models.UniqueConstraint(
                fields=("batch", "kit_definition_version"), name="paperkitver_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentryperformer",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="paperperformer_org_id"
            ),
        ),
        migrations.AddConstraint(
            model_name="paperentryperformer",
            constraint=models.UniqueConstraint(
                fields=("batch", "performer"), name="paperperformer_once"
            ),
        ),
    ]
