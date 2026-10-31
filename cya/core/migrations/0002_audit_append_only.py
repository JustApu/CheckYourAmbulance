"""Schema-owner installation of append-only guards for audit/receipt evidence.

Frozen DDL intentionally does not import mutable model or helper definitions.
Runtime grants/effective privilege verification remain issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23) follow-up.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("cya_core", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql=[
                "CREATE TRIGGER cya_command_receipt_bu BEFORE UPDATE ON command_receipt "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_command_receipt_bd BEFORE DELETE ON command_receipt "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_audit_event_bu BEFORE UPDATE ON audit_event "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
                "CREATE TRIGGER cya_audit_event_bd BEFORE DELETE ON audit_event "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'",
            ],
            reverse_sql=[
                "DROP TRIGGER cya_audit_event_bd",
                "DROP TRIGGER cya_audit_event_bu",
                "DROP TRIGGER cya_command_receipt_bd",
                "DROP TRIGGER cya_command_receipt_bu",
            ],
        ),
    ]
