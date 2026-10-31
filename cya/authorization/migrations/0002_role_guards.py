"""Schema-owner tenant, receipt, ordering and append-only role guards."""

from django.db import migrations

UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
FKS = [
    ("role_user_org_fk", "organization_id,user_id", "user", "organization_id,id"),
    ("role_actor_org_fk", "organization_id,actor_user_id", "user", "organization_id,id"),
    ("role_command_org_fk", "organization_id,command_id", "command_receipt", "organization_id,id"),
    ("role_prior_org_fk", "organization_id,prior_grant_id", "role_grant", "organization_id,id"),
]
GUARD = """CREATE TRIGGER cya_role_integrity_bi BEFORE INSERT ON role_grant
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role receipt attribution mismatch';
END IF;
IF NEW.sequence <> (SELECT COALESCE(MAX(g.sequence),0)+1 FROM role_grant g WHERE g.user_id=NEW.user_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role sequence mismatch';
END IF;
IF BINARY NEW.action='REVOKE' AND NOT EXISTS (
 SELECT 1 FROM role_grant g WHERE g.id=NEW.prior_grant_id AND g.user_id=NEW.user_id
 AND g.organization_id=NEW.organization_id AND BINARY g.role=BINARY NEW.role
 AND BINARY g.action='GRANT') THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role revocation mismatch';
END IF;
IF BINARY NEW.action='GRANT' AND EXISTS (
 SELECT 1 FROM role_grant g WHERE g.user_id=NEW.user_id AND BINARY g.role=BINARY NEW.role
 AND BINARY g.action='GRANT' AND NOT EXISTS (
 SELECT 1 FROM role_grant r WHERE r.prior_grant_id=g.id)) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'role already granted';
END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_authorization", "0001_initial"),
        ("cya_core", "0004_audit_integrity"),
    ]
    operations = [
        migrations.RunSQL(
            [
                f"ALTER TABLE role_grant ADD CONSTRAINT role_uuid CHECK (BINARY id REGEXP '{UUID}')",
                "ALTER TABLE role_grant ADD CONSTRAINT role_codes CHECK (BINARY role IN ('EMT','SUPERVISOR','SUPPLY_OFFICER','ADMINISTRATOR') AND BINARY action IN ('GRANT','REVOKE'))",
                'ALTER TABLE role_grant ADD CONSTRAINT role_actor_roles CHECK (JSON_TYPE(actor_roles_at_time)=\'ARRAY\' AND JSON_LENGTH(actor_roles_at_time)<=4 AND JSON_CONTAINS(\'["EMT","SUPERVISOR","SUPPLY_OFFICER","ADMINISTRATOR"]\',actor_roles_at_time) AND (actor_user_id IS NOT NULL OR JSON_LENGTH(actor_roles_at_time)=0))',
            ]
            + [
                f"ALTER TABLE role_grant ADD CONSTRAINT {name} FOREIGN KEY ({columns}) REFERENCES `{parent}` ({keys}) ON DELETE RESTRICT ON UPDATE RESTRICT"
                for name, columns, parent, keys in FKS
            ]
            + [
                f"CREATE TRIGGER cya_role_{operation.lower()} BEFORE {operation} ON role_grant FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
                for operation in ("UPDATE", "DELETE")
            ]
            + [GUARD],
            [
                "DROP TRIGGER cya_role_integrity_bi",
                "DROP TRIGGER cya_role_delete",
                "DROP TRIGGER cya_role_update",
            ]
            + [f"ALTER TABLE role_grant DROP FOREIGN KEY {name}" for name, *_ in reversed(FKS)]
            + [
                f"ALTER TABLE role_grant DROP CONSTRAINT {name}"
                for name in ("role_actor_roles", "role_codes", "role_uuid")
            ],
        )
    ]
