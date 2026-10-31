"""Audited definitions for explicitly shared obligation satisfaction (SPEC §5.5)."""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields


class Migration(migrations.Migration):
    dependencies = [
        ("cya_obligations", "0003_kit_instance_anchor"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_accounts", "0002_identity_guards"),
    ]

    operations = [
        migrations.CreateModel(
            name="SharedSatisfactionRule",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                ("service_identity", models.CharField(max_length=200, null=True)),
                ("job_key", models.CharField(max_length=200, null=True)),
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
                ("revision", models.PositiveIntegerField()),
                ("effective_at", models.DateTimeField()),
                (
                    "actor_user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
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
            ],
            options={
                "db_table": "shared_satisfaction_rule",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="shared_rule_org_id"
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "revision"), name="shared_rule_revision"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gte", 1)),
                        name="shared_rule_revision_min",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("actor_user__isnull", False),
                            ("job_key__isnull", True),
                            ("service_identity__isnull", True),
                        ),
                        name="shared_rule_human_only",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SharedSatisfactionRuleMember",
            fields=[
                ("recorded_at", models.DateTimeField()),
                ("actor_roles_at_time", models.JSONField(default=list)),
                ("service_identity", models.CharField(max_length=200, null=True)),
                ("job_key", models.CharField(max_length=200, null=True)),
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
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
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
                    "additional_requirement_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_obligations.requirementversion",
                    ),
                ),
                (
                    "rule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="members",
                        to="cya_obligations.sharedsatisfactionrule",
                    ),
                ),
                (
                    "source_requirement_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_obligations.requirementversion",
                    ),
                ),
            ],
            options={
                "db_table": "shared_satisfaction_rule_member",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="shared_member_org_id"
                    ),
                    models.UniqueConstraint(
                        fields=(
                            "rule",
                            "source_requirement_version",
                            "additional_requirement_version",
                        ),
                        name="shared_member_pair",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "source_requirement_version",
                                models.F("additional_requirement_version"),
                            ),
                            _negated=True,
                        ),
                        name="shared_member_distinct_versions",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("actor_user__isnull", False),
                            ("job_key__isnull", True),
                            ("service_identity__isnull", True),
                        ),
                        name="shared_member_human_only",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            [
                "ALTER TABLE `shared_satisfaction_rule` ADD CONSTRAINT `cya_shared_rule_org_fk` FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule` ADD CONSTRAINT `cya_shared_rule_receipt_fk` FOREIGN KEY (organization_id,command_id) REFERENCES `command_receipt` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_org_rule_fk` FOREIGN KEY (organization_id,rule_id) REFERENCES `shared_satisfaction_rule` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_receipt_fk` FOREIGN KEY (organization_id,command_id) REFERENCES `command_receipt` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_actor_fk` FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_source_fk` FOREIGN KEY (organization_id,source_requirement_version_id) REFERENCES `requirement_version` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_additional_fk` FOREIGN KEY (organization_id,additional_requirement_version_id) REFERENCES `requirement_version` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                'ALTER TABLE `shared_satisfaction_rule` ADD CONSTRAINT `cya_shared_rule_identity` CHECK (BINARY id REGEXP \'^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$\' AND JSON_TYPE(actor_roles_at_time)=\'ARRAY\' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\',actor_roles_at_time))',
                "ALTER TABLE `shared_satisfaction_rule_member` ADD CONSTRAINT `cya_shared_member_identity` CHECK (BINARY id REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')",
                "CREATE TRIGGER cya_shared_satisfaction_rule_bu BEFORE UPDATE ON shared_satisfaction_rule FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_shared_satisfaction_rule_bd BEFORE DELETE ON shared_satisfaction_rule FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_shared_rule_bi BEFORE INSERT ON shared_satisfaction_rule FOR EACH ROW BEGIN IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id AND r.organization_id=NEW.organization_id AND r.actor_user_id=NEW.actor_user_id AND r.recorded_at=NEW.recorded_at AND r.command_type='SHARED_SATISFACTION_RULE_SET') OR NEW.service_identity IS NOT NULL OR NEW.job_key IS NOT NULL OR JSON_CONTAINS(NEW.actor_roles_at_time, JSON_QUOTE('ADMINISTRATOR'))=0 OR NEW.revision <> COALESCE((SELECT MAX(x.revision)+1 FROM shared_satisfaction_rule x WHERE x.organization_id=NEW.organization_id),1) OR NEW.effective_at<NEW.recorded_at OR NEW.effective_at<=COALESCE((SELECT MAX(x.effective_at) FROM shared_satisfaction_rule x WHERE x.organization_id=NEW.organization_id),'1000-01-01 00:00:00.000000') THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'shared rule receipt or revision mismatch'; END IF; END",
                "CREATE TRIGGER cya_shared_satisfaction_rule_member_bu BEFORE UPDATE ON shared_satisfaction_rule_member FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_shared_satisfaction_rule_member_bd BEFORE DELETE ON shared_satisfaction_rule_member FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_shared_rule_member_bi BEFORE INSERT ON shared_satisfaction_rule_member FOR EACH ROW BEGIN IF NOT EXISTS (SELECT 1 FROM shared_satisfaction_rule r JOIN requirement_version s ON s.id=NEW.source_requirement_version_id JOIN requirement_version a ON a.id=NEW.additional_requirement_version_id JOIN command_receipt c ON c.id=NEW.command_id WHERE r.id=NEW.rule_id AND r.organization_id=NEW.organization_id AND s.organization_id=NEW.organization_id AND a.organization_id=NEW.organization_id AND s.vehicle_id=a.vehicle_id AND s.item_id=a.item_id AND s.kit_key=a.kit_key AND NEW.command_id=r.command_id AND NEW.actor_user_id=r.actor_user_id AND NEW.recorded_at=r.recorded_at AND NEW.actor_roles_at_time <=> r.actor_roles_at_time AND NEW.service_identity IS NULL AND NEW.job_key IS NULL AND c.command_type='SHARED_SATISFACTION_RULE_SET' AND c.organization_id=NEW.organization_id AND c.actor_user_id=NEW.actor_user_id AND c.recorded_at=NEW.recorded_at) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'shared rule member attribution or scope mismatch'; END IF; END",
            ],
            reverse_sql=[
                "DROP TRIGGER cya_shared_rule_member_bi",
                "DROP TRIGGER cya_shared_rule_bi",
                "DROP TRIGGER cya_shared_satisfaction_rule_member_bd",
                "DROP TRIGGER cya_shared_satisfaction_rule_member_bu",
                "DROP TRIGGER cya_shared_satisfaction_rule_bd",
                "DROP TRIGGER cya_shared_satisfaction_rule_bu",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP FOREIGN KEY `cya_shared_member_additional_fk`",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP FOREIGN KEY `cya_shared_member_actor_fk`",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP FOREIGN KEY `cya_shared_member_receipt_fk`",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP CONSTRAINT `cya_shared_member_identity`",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP FOREIGN KEY `cya_shared_member_source_fk`",
                "ALTER TABLE `shared_satisfaction_rule_member` DROP FOREIGN KEY `cya_shared_member_org_rule_fk`",
                "ALTER TABLE `shared_satisfaction_rule` DROP FOREIGN KEY `cya_shared_rule_receipt_fk`",
                "ALTER TABLE `shared_satisfaction_rule` DROP FOREIGN KEY `cya_shared_rule_org_fk`",
                "ALTER TABLE `shared_satisfaction_rule` DROP CONSTRAINT `cya_shared_rule_identity`",
            ],
        ),
    ]
