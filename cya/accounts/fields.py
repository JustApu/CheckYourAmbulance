"""Contract UUID representation shared by identity and future evidence rows.

Same representation as the identity anchors drafted for issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22) (PR 42 (https://github.com/JustApu/CheckYourAmbulance-archive/pull/42)),
so both issues share one schema: MariaDB CHAR(36) in the ASCII character set
with binary collation, holding a canonical lowercase UUID string
(docs/data-model-contracts.md section 1).
"""

from uuid import uuid4

from django.core.validators import RegexValidator
from django.db import models

UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def new_id():
    return str(uuid4())


class IdentityIDField(models.CharField):
    """MariaDB CHAR(36), ASCII binary, canonical lowercase UUID on the wire."""

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 36
        kwargs["db_collation"] = "ascii_bin"
        super().__init__(*args, **kwargs)
        self.validators.append(
            RegexValidator(
                r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
                "A canonical lowercase UUID is required.",
            )
        )

    def db_type(self, connection):
        if connection.vendor != "mysql":
            raise NotImplementedError("Identity schema requires MariaDB 10.11.")
        return "char(36) CHARACTER SET ascii"
