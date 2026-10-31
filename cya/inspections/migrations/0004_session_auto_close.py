"""Let the auto-close job write session events without an actor user (issue 78 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/78) Part B).

An AUTO_INCOMPLETE event is the job's, so session_event.actor_user becomes nullable and a CHECK
ties null to that one event type. MariaDB refuses to change a column an organization-pair key
uses (error 1832), so that key from 0003 is dropped around the change and added back. Reversing
needs every actor present, so it stops (the column cannot become NOT NULL) if an auto-close
exists: a recorded close is never discarded.
"""

import django.db.models.deletion
from django.db import migrations, models

ACTOR_KEY = "cya_sess_org_fk_10"  # (organization_id, actor_user_id) on session_event, from 0003
SINGLE_KEY = "cya_session_event_actor_user_fk"
COLUMN = "char(36) CHARACTER SET ascii COLLATE ascii_bin"


def change_actor_nullability(schema_editor, *, nullable):
    """Change the column by hand: Django's MODIFY would drop the ascii_bin collation (error 150)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE `session_event` DROP FOREIGN KEY `{ACTOR_KEY}`")
        cursor.execute(
            "SELECT DISTINCT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='session_event' "
            "AND COLUMN_NAME='actor_user_id' AND REFERENCED_TABLE_NAME='user'"
        )
        for (name,) in cursor.fetchall():
            cursor.execute(f"ALTER TABLE `session_event` DROP FOREIGN KEY `{name}`")
        cursor.execute(
            f"ALTER TABLE `session_event` MODIFY `actor_user_id` {COLUMN} "
            f"{'NULL' if nullable else 'NOT NULL'}"
        )
        cursor.execute(
            f"ALTER TABLE `session_event` ADD CONSTRAINT `{SINGLE_KEY}` "
            "FOREIGN KEY (actor_user_id) REFERENCES `user` (id)"
        )
        cursor.execute(
            f"ALTER TABLE `session_event` ADD CONSTRAINT `{ACTOR_KEY}` "
            "FOREIGN KEY (organization_id,actor_user_id) REFERENCES `user` (organization_id,id) "
            "ON DELETE RESTRICT ON UPDATE RESTRICT"
        )


def make_nullable(apps, schema_editor):
    change_actor_nullability(schema_editor, nullable=True)


def make_required(apps, schema_editor):
    change_actor_nullability(schema_editor, nullable=False)


class Migration(migrations.Migration):
    dependencies = [
        ("cya_accounts", "0002_identity_guards"),
        ("cya_core", "0005_audit_roles_crew"),
        ("cya_inspections", "0003_session_guards"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="sessionevent",
            name="session_event_type",
        ),
        migrations.RemoveConstraint(
            model_name="sessionevent",
            name="session_event_activity_shape",
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(make_nullable, make_required)],
            state_operations=[
                migrations.AlterField(
                    model_name="sessionevent",
                    name="actor_user",
                    field=models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="cya_accounts.user",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "event_type__in",
                        (
                            "OPEN",
                            "JOIN",
                            "REOPEN",
                            "END_INCOMPLETE",
                            "END_CLOSED",
                            "AUTO_INCOMPLETE",
                        ),
                    )
                ),
                name="session_event_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("event_type__in", ["OPEN", "REOPEN"]),
                        ("persisted_activity_command__isnull", False),
                    ),
                    models.Q(
                        (
                            "event_type__in",
                            ["JOIN", "END_INCOMPLETE", "END_CLOSED", "AUTO_INCOMPLETE"],
                        ),
                        ("persisted_activity_command__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="session_event_activity_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("actor_user__isnull", True), ("event_type", "AUTO_INCOMPLETE")),
                    models.Q(
                        models.Q(("event_type", "AUTO_INCOMPLETE"), _negated=True),
                        ("actor_user__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="session_event_actor_shape",
            ),
        ),
    ]
