"""Write the host copy of the outgoing-mail relay for the host alerts (SPEC DEP-13, issue #8).

Run as root by cya-mail-relay-export.timer (at least hourly) and after each change. The file is
written mode 0600 through a temporary file in the same directory and an atomic rename, so the host
alerts always read either the previous good copy or the new one, never a partial file. On any
failure the previous copy is left in place and the command exits nonzero, which the timer's
failure hook alerts on.

The organization is the one designated for server operations (in the first release, the only
organization). The command prints only the revision written, never the settings.
"""

import os
import tempfile

from django.core.management.base import BaseCommand, CommandError

from cya.authorization.backend import canonical_id
from cya.notifications.models import MailRelay
from cya.notifications.relay import SendFailure, host_copy


class Command(BaseCommand):
    help = "Write the outgoing-mail relay host copy (root-owned, mode 0600) for host alerts."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True)
        parser.add_argument("--output", required=True)

    def handle(self, *args, organization, output, **options):
        try:
            canonical_id(organization)
        except ValueError:
            raise CommandError("--organization must be a canonical UUID") from None
        relay = MailRelay.objects.filter(organization_id=organization).first()
        if relay is None:
            raise CommandError("no relay is configured for that organization; nothing written")
        try:
            document = host_copy(relay, organization)
        except SendFailure:
            raise CommandError(
                "the relay password cannot be decrypted (CYA_MAIL_RELAY_KEY missing or wrong); "
                "the previous host copy is unchanged"
            ) from None
        directory = os.path.dirname(os.path.abspath(output))
        descriptor, temporary = tempfile.mkstemp(prefix=".mail-relay.", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(document + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        self.stdout.write(f"mail relay host copy written: revision {relay.revision}")
