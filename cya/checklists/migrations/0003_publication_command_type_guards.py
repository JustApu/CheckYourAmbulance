"""Enforce command types in checklist publication and stocking deviation insert triggers.

Extends cya_checklist_publication_bi and cya_stocking_deviation_bi to require that
the referenced command_receipt row has an authorized command_type.
"""

from django.db import migrations


def receipt_predicate(allowed_types, actor_col="actor_user_id"):
    if isinstance(allowed_types, str):
        type_check = f"r.command_type = '{allowed_types}'"
    else:
        types_str = ", ".join(f"'{t}'" for t in allowed_types)
        type_check = f"r.command_type IN ({types_str})"
    return (
        f"r.id=NEW.command_id\n"
        f" AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.{actor_col})\n"
        f" AND r.recorded_at=NEW.recorded_at\n"
        f" AND {type_check}"
    )


PUBLICATION_INSERT_NEW = f"""CREATE TRIGGER cya_checklist_publication_bi BEFORE INSERT ON checklist_publication
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE
 {receipt_predicate(("CHECKLIST_PUBLISH", "OVERRIDE_PUBLISH"))}) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'publication receipt attribution mismatch';
END IF;
IF NEW.checklist_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM checklist_item_def d WHERE d.checklist_version_id=NEW.checklist_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty checklist version';
END IF;
IF NEW.override_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_override_item i WHERE i.override_version_id=NEW.override_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty override version';
END IF;
END"""

DEVIATION_INSERT_NEW = f"""CREATE TRIGGER cya_stocking_deviation_bi BEFORE INSERT ON stocking_deviation
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE
 {receipt_predicate("STOCKING_DEVIATION_RECORD", actor_col="approver_id")}) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation receipt attribution mismatch';
END IF;
IF NEW.prior_revision_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM stocking_deviation d WHERE d.id=NEW.prior_revision_id
 AND d.vehicle_id=NEW.vehicle_id AND d.item_id=NEW.item_id AND d.revision=NEW.revision-1) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation revision chain mismatch';
END IF;
END"""

PUBLICATION_INSERT_OLD = """CREATE TRIGGER cya_checklist_publication_bi BEFORE INSERT ON checklist_publication
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.actor_user_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'publication receipt attribution mismatch';
END IF;
IF NEW.checklist_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM checklist_item_def d WHERE d.checklist_version_id=NEW.checklist_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty checklist version';
END IF;
IF NEW.override_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM vehicle_override_item i WHERE i.override_version_id=NEW.override_version_id) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'cannot publish an empty override version';
END IF;
END"""

DEVIATION_INSERT_OLD = """CREATE TRIGGER cya_stocking_deviation_bi BEFORE INSERT ON stocking_deviation
FOR EACH ROW BEGIN
IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.organization_id=NEW.organization_id AND (r.actor_user_id <=> NEW.approver_id)
 AND r.recorded_at=NEW.recorded_at) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation receipt attribution mismatch';
END IF;
IF NEW.prior_revision_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM stocking_deviation d WHERE d.id=NEW.prior_revision_id
 AND d.vehicle_id=NEW.vehicle_id AND d.item_id=NEW.item_id AND d.revision=NEW.revision-1) THEN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'deviation revision chain mismatch';
END IF;
END"""


class Migration(migrations.Migration):
    dependencies = [
        ("cya_checklists", "0002_publication_guards"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TRIGGER IF EXISTS cya_checklist_publication_bi",
                "DROP TRIGGER IF EXISTS cya_stocking_deviation_bi",
                PUBLICATION_INSERT_NEW,
                DEVIATION_INSERT_NEW,
            ],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS cya_checklist_publication_bi",
                "DROP TRIGGER IF EXISTS cya_stocking_deviation_bi",
                PUBLICATION_INSERT_OLD,
                DEVIATION_INSERT_OLD,
            ],
        ),
    ]
