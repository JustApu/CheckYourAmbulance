"""Database guards for identity rows (contracts section 3).

Same guards as PR 42 (https://github.com/JustApu/CheckYourAmbulance-archive/pull/42)'s identity anchors: canonical UUID ids, non-blank
identity tuple, immutable identity columns and no user deletion (attribution
is kept). NULL-safe, binary comparisons (BINARY ... <=>) mean neither NULL
changes nor case-only changes slip through. SIGNAL SQLSTATE '45000' raises an
error in the caller's transaction.
"""

from django.db import migrations

from cya.accounts.fields import UUID_PATTERN

USER_IDENTITY_TRIGGER = """
CREATE TRIGGER user_identity_immutable BEFORE UPDATE ON `user` FOR EACH ROW
IF NOT (BINARY OLD.id <=> BINARY NEW.id)
   OR NOT (BINARY OLD.organization_id <=> BINARY NEW.organization_id)
   OR NOT (BINARY OLD.idp_issuer <=> BINARY NEW.idp_issuer)
   OR NOT (BINARY OLD.idp_subject <=> BINARY NEW.idp_subject) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Identity anchors are immutable';
END IF
"""

USER_DELETE_TRIGGER = """
CREATE TRIGGER user_retain_attribution BEFORE DELETE ON `user` FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Retain inactive user attribution'
"""

ORGANIZATION_TRIGGER = """
CREATE TRIGGER organization_identity_immutable BEFORE UPDATE ON organization FOR EACH ROW
IF NOT (BINARY OLD.id <=> BINARY NEW.id) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Organization identity is immutable';
END IF
"""


def sql(statement, reverse):
    # One-item lists: a plain string would be split at the semicolons inside
    # a trigger body (Django splits SQL scripts into statements).
    return migrations.RunSQL([statement], reverse_sql=[reverse])


class Migration(migrations.Migration):
    dependencies = [("cya_accounts", "0001_initial")]

    operations = [
        sql(
            f"ALTER TABLE organization ADD CONSTRAINT org_uuid CHECK (id REGEXP BINARY '{UUID_PATTERN}')",
            "ALTER TABLE organization DROP CONSTRAINT org_uuid",
        ),
        sql(
            f"ALTER TABLE `user` ADD CONSTRAINT user_uuid CHECK (id REGEXP BINARY '{UUID_PATTERN}')",
            "ALTER TABLE `user` DROP CONSTRAINT user_uuid",
        ),
        sql(
            "ALTER TABLE `user` ADD CONSTRAINT user_identity_nonempty CHECK "
            "(CHAR_LENGTH(TRIM(idp_issuer)) > 0 AND CHAR_LENGTH(TRIM(idp_subject)) > 0)",
            "ALTER TABLE `user` DROP CONSTRAINT user_identity_nonempty",
        ),
        sql(USER_IDENTITY_TRIGGER, "DROP TRIGGER user_identity_immutable"),
        sql(USER_DELETE_TRIGGER, "DROP TRIGGER user_retain_attribution"),
        sql(ORGANIZATION_TRIGGER, "DROP TRIGGER organization_identity_immutable"),
    ]
