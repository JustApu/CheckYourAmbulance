"""Pure validation of command identity, independent of Django bootstrap.

Database constraints and authorization must additionally enforce these rules.
"""

from __future__ import annotations

import json
import uuid


def canonical_uuid(value: str, *, version: int | None = None) -> uuid.UUID:
    if not isinstance(value, str):
        raise ValueError("UUID must be a canonical string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid UUID") from exc
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise ValueError("noncanonical UUID or invalid variant")
    if version is not None and parsed.version != version:
        raise ValueError(f"expected UUID version {version}")
    return parsed


def human_operation_id(value: str) -> uuid.UUID:
    return canonical_uuid(value, version=4)


def worker_operation_id(
    organization_id: str, service_identity: str, command_type: str, job_key: str
) -> uuid.UUID:
    """SPEC §18: UUID5 of compact ASCII JSON of four ordered strings."""
    namespace = canonical_uuid(organization_id)
    if not all(isinstance(x, str) and x.strip() for x in (service_identity, command_type, job_key)):
        raise ValueError("worker identity, command and job key must be nonempty")
    # The job key itself must be a compact JSON array of canonical strings.
    try:
        parts = json.loads(job_key)
    except (TypeError, ValueError) as exc:
        raise ValueError("job_key must be a JSON array") from exc
    if not isinstance(parts, list) or not parts or not all(isinstance(x, str) for x in parts):
        raise ValueError("job_key must be a nonempty string array")
    if json.dumps(parts, ensure_ascii=True, separators=(",", ":")) != job_key:
        raise ValueError("job_key must use compact ASCII JSON")
    name = json.dumps(
        ["cya-job-v1", service_identity, command_type, job_key],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(namespace, name)


def receipt_identity(
    *,
    operation_id: str,
    actor_user_id: str | None,
    service_identity: str | None,
    job_key: str | None,
) -> str:
    """Reject mixed human/service receipt attribution and UUID keyspace overlap."""
    if actor_user_id is not None:
        if service_identity is not None or job_key is not None:
            raise ValueError("human receipt cannot contain service identity")
        canonical_uuid(actor_user_id)
        human_operation_id(operation_id)
        return "human"
    if not service_identity or not job_key:
        raise ValueError("service receipt requires service_identity and job_key")
    canonical_uuid(operation_id, version=5)
    return "worker"
