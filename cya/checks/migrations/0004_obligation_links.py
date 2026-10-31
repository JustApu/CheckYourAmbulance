"""Add item serialization locks and attributed check-to-obligation links (issue 74 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/74))."""

import django.db.models.deletion
from django.db import migrations, models

import cya.accounts.fields
from tools.audit_enforcement import immutable_header_guard

ORG = "organization_id"
APPEND = "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checks", "0003_check_session"),
        ("cya_obligations", "0004_shared_satisfaction_rule"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemLock",
            fields=[
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.organization",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_checklists.item",
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
                "db_table": "item_lock",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "vehicle", "item"), name="item_lock_scope"
                    ),
                    models.UniqueConstraint(fields=("organization", "id"), name="item_lock_org_id"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CheckObligationLink",
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
                ("ordinal", models.PositiveIntegerField()),
                (
                    "actor_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
                (
                    "check_record",
                    models.ForeignKey(
                        db_column="check_id",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="obligation_links",
                        to="cya_checks.checkrecord",
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
                    "obligation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="check_links",
                        to="cya_obligations.obligation",
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
                    "shared_rule",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_obligations.sharedsatisfactionrule",
                    ),
                ),
            ],
            options={
                "db_table": "check_obligation_link",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "id"), name="check_link_org_id"
                    ),
                    models.UniqueConstraint(
                        fields=("check_record", "obligation"), name="check_link_pair"
                    ),
                    models.UniqueConstraint(
                        fields=("check_record", "ordinal"), name="check_link_ordinal"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("ordinal__gte", 0)), name="check_link_ordinal_min"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("ordinal", 0), ("shared_rule__isnull", True)),
                            models.Q(("ordinal__gt", 0), ("shared_rule__isnull", False)),
                            _connector="OR",
                        ),
                        name="check_link_shared_shape",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            [
                "ALTER TABLE `item_lock` ADD CONSTRAINT `cya_item_lock_vehicle_org_fk` FOREIGN KEY (organization_id,vehicle_id) REFERENCES `vehicle` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `item_lock` ADD CONSTRAINT `cya_item_lock_item_org_fk` FOREIGN KEY (organization_id,item_id) REFERENCES `item` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `item_lock` ADD CONSTRAINT `cya_item_lock_uuid` CHECK (BINARY id REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')",
                immutable_header_guard("item_lock"),
                "CREATE TRIGGER cya_item_lock_bd BEFORE DELETE ON item_lock FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'item lock is permanent'",
                "ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_receipt_fk` FOREIGN KEY (organization_id,command_id) REFERENCES `command_receipt` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_actor_fk` FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_check_fk` FOREIGN KEY (organization_id,check_id) REFERENCES `check_record` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_obligation_fk` FOREIGN KEY (organization_id,obligation_id) REFERENCES `obligation` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                "ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_rule_fk` FOREIGN KEY (organization_id,shared_rule_id) REFERENCES `shared_satisfaction_rule` (organization_id,id) ON DELETE RESTRICT ON UPDATE RESTRICT",
                'ALTER TABLE `check_obligation_link` ADD CONSTRAINT `cya_check_link_identity` CHECK (BINARY id REGEXP \'^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$\' AND JSON_TYPE(actor_roles_at_time)=\'ARRAY\' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS(\'["CREW","EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\',actor_roles_at_time))',
                "CREATE TRIGGER cya_check_obligation_link_bu BEFORE UPDATE ON check_obligation_link FOR EACH ROW "
                + APPEND,
                "CREATE TRIGGER cya_check_obligation_link_bd BEFORE DELETE ON check_obligation_link FOR EACH ROW "
                + APPEND,
                "CREATE TRIGGER cya_check_obligation_link_bi BEFORE INSERT ON check_obligation_link FOR EACH ROW BEGIN IF NOT EXISTS (SELECT 1 FROM check_record c JOIN obligation o ON o.id=NEW.obligation_id JOIN command_receipt r ON r.id=NEW.command_id WHERE c.id=NEW.check_id AND c.organization_id=NEW.organization_id AND o.organization_id=NEW.organization_id AND c.command_id=NEW.command_id AND c.recorded_at=NEW.recorded_at AND c.actor_user_id=NEW.actor_user_id AND o.vehicle_id=c.vehicle_id AND o.item_id=c.item_id AND o.kit_key=COALESCE(c.kit_id,'00000000-0000-0000-0000-000000000000') AND EXISTS (SELECT 1 FROM obligation_selection s WHERE s.selected_obligation_id=o.id AND s.organization_id=o.organization_id AND s.vehicle_id=o.vehicle_id AND s.item_id=o.item_id AND s.kit_key=o.kit_key AND s.due_at=o.due_at AND s.recorded_at<=NEW.recorded_at AND s.sequence=(SELECT MAX(x.sequence) FROM obligation_selection x WHERE x.organization_id=s.organization_id AND x.vehicle_id=s.vehicle_id AND x.item_id=s.item_id AND x.kit_key=s.kit_key AND x.due_at=s.due_at AND x.recorded_at<=NEW.recorded_at)) AND r.command_type='CHECK_RECORD' AND r.organization_id=NEW.organization_id AND r.recorded_at=NEW.recorded_at AND r.actor_user_id=NEW.actor_user_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check obligation link scope mismatch'; END IF; IF NEW.ordinal=0 AND EXISTS (SELECT 1 FROM check_obligation_link x WHERE x.check_id=NEW.check_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'primary link must be inserted first'; END IF; IF NEW.ordinal>0 AND (NOT EXISTS (SELECT 1 FROM check_obligation_link x WHERE x.check_id=NEW.check_id AND x.ordinal=0) OR NOT EXISTS (SELECT 1 FROM shared_satisfaction_rule r JOIN shared_satisfaction_rule_member m ON m.rule_id=r.id JOIN obligation p ON p.id=(SELECT x.obligation_id FROM check_obligation_link x WHERE x.check_id=NEW.check_id AND x.ordinal=0) JOIN obligation a ON a.id=NEW.obligation_id WHERE r.id=NEW.shared_rule_id AND r.organization_id=NEW.organization_id AND r.recorded_at<=NEW.recorded_at AND r.effective_at<=NEW.recorded_at AND r.id=(SELECT x.id FROM shared_satisfaction_rule x WHERE x.organization_id=NEW.organization_id AND x.recorded_at<=NEW.recorded_at AND x.effective_at<=NEW.recorded_at ORDER BY x.effective_at DESC,x.revision DESC,x.id DESC LIMIT 1) AND m.source_requirement_version_id=p.requirement_version_id AND m.additional_requirement_version_id=a.requirement_version_id AND p.vehicle_id=a.vehicle_id AND p.item_id=a.item_id AND p.kit_key=a.kit_key)) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'additional link lacks an active shared rule'; END IF; END",
                """
                INSERT INTO item_lock (id,organization_id,vehicle_id,item_id)
                SELECT UUID(),scope.organization_id,scope.vehicle_id,scope.item_id
                FROM (
                    SELECT organization_id,vehicle_id,item_id FROM check_record
                    UNION
                    SELECT organization_id,vehicle_id,item_id FROM requirement_version
                    UNION
                    SELECT v.organization_id,v.id,d.item_id
                    FROM vehicle v
                    JOIN checklist_version cv ON cv.organization_id=v.organization_id
                    JOIN checklist_publication p
                      ON p.organization_id=cv.organization_id AND p.checklist_version_id=cv.id
                    JOIN checklist_item_def d
                      ON d.organization_id=cv.organization_id AND d.checklist_version_id=cv.id
                    WHERE v.active=1
                    UNION
                    SELECT ov.organization_id,ov.vehicle_id,d.item_id
                    FROM vehicle_override_version ov
                    JOIN checklist_publication p
                      ON p.organization_id=ov.organization_id AND p.override_version_id=ov.id
                    JOIN vehicle_override_item e
                      ON e.organization_id=ov.organization_id AND e.override_version_id=ov.id
                      AND e.action IN ('ADD','REPLACE')
                    JOIN checklist_item_def d
                      ON d.organization_id=ov.organization_id AND d.id=e.definition_id
                ) scope
                """,
            ],
            reverse_sql=[
                "DROP TRIGGER cya_check_obligation_link_bi",
                "DROP TRIGGER cya_check_obligation_link_bd",
                "DROP TRIGGER cya_check_obligation_link_bu",
                "ALTER TABLE `check_obligation_link` DROP FOREIGN KEY `cya_check_link_rule_fk`",
                "ALTER TABLE `check_obligation_link` DROP FOREIGN KEY `cya_check_link_obligation_fk`",
                "ALTER TABLE `check_obligation_link` DROP FOREIGN KEY `cya_check_link_check_fk`",
                "ALTER TABLE `check_obligation_link` DROP FOREIGN KEY `cya_check_link_actor_fk`",
                "ALTER TABLE `check_obligation_link` DROP FOREIGN KEY `cya_check_link_receipt_fk`",
                "ALTER TABLE `check_obligation_link` DROP CONSTRAINT `cya_check_link_identity`",
                "DROP TRIGGER cya_item_lock_immutable_bu",
                "DROP TRIGGER cya_item_lock_bd",
                "ALTER TABLE `item_lock` DROP CONSTRAINT `cya_item_lock_uuid`",
                "ALTER TABLE `item_lock` DROP FOREIGN KEY `cya_item_lock_item_org_fk`",
                "ALTER TABLE `item_lock` DROP FOREIGN KEY `cya_item_lock_vehicle_org_fk`",
            ],
        ),
    ]
