"""Transaction boundary for audited commands (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)).

This module is infrastructure, not an HTTP command handler. Callers remain
responsible for authentication, authorization, lock ordering, receipt replay,
schema validation, and typed audit rows. No business write may happen outside
the successful transaction callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.db import InterfaceError, OperationalError, transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Failure:
    outcome: str
    reason_code: str


class AuditPersistenceFailure(RuntimeError):
    """Required audit did not persist; command outcome may be unknown."""


def execute_audited_command[T](
    *,
    reserve_receipt: Callable[[], None],
    write_business_and_audit: Callable[[], T],
    record_failure_audit: Callable[[Failure], None],
) -> T:
    """Reserve receipt first, then commit business effects and success audit atomically.

    Low-level boundary only: use audit_commands.run_command for receipt replay
    and bounded retries. Callbacks receive safe failure metadata, never raw SQL.
    On an ordinary exception, the entire transaction (including receipt) rolls back.
    Record the failure in a separate transaction, not in the rolled-back one.
    A failed audit is escalated; callers must never report a successful audit.
    """
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Audit boundary must be outermost")
    committed, completed, result = [], False, None
    try:
        with transaction.atomic(durable=True):
            transaction.on_commit(lambda: committed.append(True), robust=True)
            reserve_receipt()
            result = write_business_and_audit()
            completed = True
        return result
    except Exception as original:
        if committed and completed:
            logger.error("post-commit callback failed; command remains committed")
            return result
        unknown = isinstance(original, InterfaceError) or (
            isinstance(original, OperationalError)
            and original.args
            and original.args[0] in (2006, 2013, 2055)
        )
        failure = (
            Failure("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN")
            if unknown
            else Failure("FAILED", "COMMAND_FAILED")
        )
        if unknown:
            transaction.get_connection().close()
        try:
            with transaction.atomic(durable=True):
                record_failure_audit(failure)
        except Exception as audit_error:
            logger.error("mandatory audit persistence unavailable")
            raise AuditPersistenceFailure(
                "AUDIT_UNAVAILABLE; command outcome requires reconciliation"
            ) from audit_error
        raise
