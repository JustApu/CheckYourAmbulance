"""Schema-owner guards for presence items (SPEC 2.0.0 4.6 KDEF-12, issue #129).

- An organization-pair key keeps a definition's presence item in its own organization.
- A definition's presence item never changes (a separate trigger, so migration 0005's key guard,
  generated from tools.audit_enforcement.IMMUTABLE_HEADERS, stays as it was).
- A presence item is never kit content: kit_definition_item refuses it on insert.

Each trigger is one list element so Django runs it whole.
"""

from django.db import migrations

ORG = "organization_id"
FK = "cya_kit_definition_presence_item_id_org_fk"


def signal(message):
    return f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"


FORWARD = [
    f"ALTER TABLE `kit_definition` ADD CONSTRAINT `{FK}` FOREIGN KEY ({ORG},presence_item_id) "
    f"REFERENCES `item` ({ORG},id) ON DELETE RESTRICT ON UPDATE RESTRICT",
    "CREATE TRIGGER cya_kit_definition_presence_bu BEFORE UPDATE ON kit_definition FOR EACH ROW "
    "BEGIN IF NOT (BINARY OLD.presence_item_id <=> BINARY NEW.presence_item_id) THEN "
    f"{signal('kit definition presence item is immutable')}; END IF; END",
    "CREATE TRIGGER cya_kit_definition_item_presence_bi BEFORE INSERT ON kit_definition_item "
    "FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM kit_definition d "
    "WHERE d.presence_item_id = NEW.item_id) THEN "
    f"{signal('a presence item is never kit content')}; END IF; END",
]

REVERSE = [
    "DROP TRIGGER cya_kit_definition_item_presence_bi",
    "DROP TRIGGER cya_kit_definition_presence_bu",
    f"ALTER TABLE `kit_definition` DROP FOREIGN KEY `{FK}`",
    f"ALTER TABLE `kit_definition` DROP INDEX `{FK}`",
]


class Migration(migrations.Migration):
    dependencies = [("cya_kits", "0009_kit_definition_presence_item")]

    operations = [migrations.RunSQL(FORWARD, reverse_sql=REVERSE)]
