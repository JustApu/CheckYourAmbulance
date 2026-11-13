"""Audited commands for kit definitions (SPEC 4.6 KDEF-1 to KDEF-8, issue #129).

A kit definition is a kit type whose contents are versioned like a checklist template
(CHK-2, CHK-3): every save inserts the next draft revision with its complete content set, a
publication freezes one revision at an effective instant never earlier than the publication,
and publications of one definition are strictly ordered. Duplicating copies one version's
contents into a new definition's first draft, with none of the original's history. A
deactivated definition cannot be given to a new kit instance (cya.kits.commands).

All commands need the policy action manage_kit_definitions (Supervisors and Administrators,
SPEC 3.3) and run through cya.core.audit_commands.run_command: one transaction, an
operation-id receipt that replays the original result, a success or failure audit. Do not wrap
them in a transaction. The definition row is locked FOR UPDATE before validation, so
concurrent saves get distinct revisions and a publish cannot race another publish.

Obligations do not use kit definitions yet: placing kits by definition in a checklist
(KDEF-4) is a later slice of issue #129, so publishing a definition changes no obligation.
"""

from datetime import datetime

from django.db.models import Max

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import OUTCOMES, Item
from cya.checklists.validation import MAX_INSTRUCTIONS, MAX_UNIT, is_count
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits.models import (
    KIT_KINDS,
    MAX_KEY,
    KitDefinition,
    KitDefinitionItem,
    KitDefinitionPublication,
    KitDefinitionVersion,
)

ACTION = "manage_kit_definitions"
CREATE_COMMAND = "KIT_DEFINITION_CREATE"
DRAFT_COMMAND = "KIT_DEFINITION_DRAFT"
PUBLISH_COMMAND = "KIT_DEFINITION_PUBLISH"
DUPLICATE_COMMAND = "KIT_DEFINITION_DUPLICATE"
ACTIVE_COMMAND = "KIT_DEFINITION_ACTIVE"
MAX_CONTENTS = 500
MAX_LABEL = 200
CONTENT_FIELDS = frozenset(
    {
        "item_id",
        "display_order",
        "kind",
        "instructions",
        "expected_quantity",
        "unit",
        "allowed_outcomes",
        "expiration_tracked",
    }
)
CONTENT_REQUIRED = frozenset({"item_id", "display_order", "kind", "allowed_outcomes"})


class KitDefinitionError(ValueError):
    """A rule was violated; the command rolls back and is audited as failed."""


# --- shared command plumbing -----------------------------------------------------------------


def _authorize(organization_id, actor_id):
    backend = DatabaseRoleBackend()

    def authorize(_org):
        with locked_identities(organization_id, [actor_id], lock_organization=False) as (
            current_org,
            users,
        ):
            user = users[actor_id]
            roles = frozenset(
                Role.CREW if r == Role.EMT else r for r in load_state(backend, user).grants
            )
            actor = Actor(user=user, roles=tuple(sorted(r.value for r in roles)))
            principal = Principal(
                organization_id, actor_id, roles, user.active and current_org.active
            )
            try:
                require_permission(principal, organization_id, ACTION)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _run(context, command_type, request, build):
    canonical_id(context["actor_id"])
    return run_command(
        organization_id=context["organization_id"],
        operation_id=context["operation_id"],
        command_type=command_type,
        request_digest=digest(request),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(context["organization_id"], context["actor_id"]),
        prepare=build,
    )


def _plan(organization_id, body, apply, **values):
    return Plan(
        response_body=body,
        apply=apply,
        target_type="ORGANIZATION",
        target_id=organization_id,
        new_snapshot={"schema_version": 1, "values": values},
    )


def _locked_definition(org, definition_id):
    definition = (
        KitDefinition.objects.select_for_update()
        .filter(organization_id=org, pk=definition_id)
        .first()
    )
    if definition is None:
        raise KitDefinitionError(
            f"kit definition {definition_id} does not exist in the organization"
        )
    return definition


def _is_published(version_id):
    return KitDefinitionPublication.objects.filter(version_id=version_id).exists()


# --- validation ------------------------------------------------------------------------------


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_KEY:
        raise KitDefinitionError(f"key must be text of 1-{MAX_KEY} characters")
    first, rest = value[0], value[1:]
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789")
    if first not in allowed or any(c not in allowed | set("._-") for c in rest):
        raise KitDefinitionError(
            "key must start with a lowercase letter or digit and use only a-z, 0-9, '.', '_', '-'"
        )
    return value


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_LABEL:
        raise KitDefinitionError(f"label must be nonblank text of at most {MAX_LABEL} characters")
    return value


def _outcomes(entry):
    outcomes = entry["allowed_outcomes"]
    if (
        not isinstance(outcomes, list)
        or not 1 <= len(outcomes) <= len(OUTCOMES)
        or len(set(outcomes)) != len(outcomes)
        or any(o not in OUTCOMES for o in outcomes)
        or "PASSED" not in outcomes
    ):
        raise KitDefinitionError(
            f"allowed_outcomes must be distinct values from {list(OUTCOMES)} including PASSED"
        )
    return list(outcomes)


def _quantity_and_unit(entry):
    kind, quantity, unit = entry["kind"], entry.get("expected_quantity"), entry.get("unit")
    if kind == "QUANTITY":
        if not is_count(quantity):
            raise KitDefinitionError("QUANTITY contents need a nonnegative integer quantity")
    elif quantity is not None:
        raise KitDefinitionError(f"{kind} contents must not carry an expected quantity")
    if kind == "MEASUREMENT":
        if not isinstance(unit, str) or not unit.strip() or len(unit) > MAX_UNIT:
            raise KitDefinitionError(f"MEASUREMENT contents need a unit of 1-{MAX_UNIT} characters")
    elif unit is not None:
        raise KitDefinitionError(f"{kind} contents must not carry a unit")
    return quantity, unit


def _content(org, index, entry):
    """Model fields for one content entry, or raise naming its position."""
    where = f"contents[{index}]"
    if not isinstance(entry, dict):
        raise KitDefinitionError(f"{where} must be an object")
    unknown, missing = set(entry) - CONTENT_FIELDS, CONTENT_REQUIRED - set(entry)
    if unknown or missing:
        raise KitDefinitionError(
            f"{where} fields rejected: unknown={sorted(unknown)} missing={sorted(missing)}"
        )
    if entry["kind"] not in KIT_KINDS:
        raise KitDefinitionError(f"{where} kind must be one of {list(KIT_KINDS)}")
    if not is_count(entry["display_order"]):
        raise KitDefinitionError(f"{where} display_order must be a nonnegative integer")
    instructions = entry.get("instructions", "")
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        raise KitDefinitionError(f"{where} instructions must be text of at most {MAX_INSTRUCTIONS}")
    tracked = entry.get("expiration_tracked", False)
    if type(tracked) is not bool:
        raise KitDefinitionError(f"{where} expiration_tracked must be true or false")
    try:
        item_id = canonical_id(entry["item_id"])
    except (TypeError, ValueError):
        raise KitDefinitionError(f"{where} item_id must be a canonical UUID") from None
    item = Item.objects.filter(organization_id=org, pk=item_id).first()
    if item is None or not item.active:
        raise KitDefinitionError(
            f"{where} item {item_id} is not an active item of the organization"
        )
    try:
        quantity, unit = _quantity_and_unit(entry)
        outcomes = _outcomes(entry)
    except KitDefinitionError as error:
        raise KitDefinitionError(f"{where} {error}") from None
    return {
        "item_id": item_id,
        "display_order": entry["display_order"],
        "kind": entry["kind"],
        "instructions": instructions,
        "expected_quantity": quantity,
        "unit": unit,
        "allowed_outcomes": outcomes,
        "expiration_tracked": tracked,
    }


def validate_contents(org, contents):
    """A complete content set: bounded, nonempty, one entry per item (KDEF-1, KDEF-5)."""
    if not isinstance(contents, list) or not 1 <= len(contents) <= MAX_CONTENTS:
        raise KitDefinitionError(f"provide 1-{MAX_CONTENTS} content entries")
    validated = [_content(org, index, entry) for index, entry in enumerate(contents)]
    item_ids = [entry["item_id"] for entry in validated]
    if len(set(item_ids)) != len(item_ids):
        raise KitDefinitionError("an item may appear only once in a kit definition version")
    return validated


def _aware(value):
    if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
        raise KitDefinitionError("effective_at must be a timezone-aware datetime")
    return value


# --- commands --------------------------------------------------------------------------------


def _insert_version(org, definition, revision, version_id, contents, created_at):
    KitDefinitionVersion.objects.create(
        id=version_id,
        organization_id=org,
        definition=definition,
        revision=revision,
        created_at=created_at,
    )
    KitDefinitionItem.objects.bulk_create(
        KitDefinitionItem(id=new_id(), organization_id=org, version_id=version_id, **fields)
        for fields in contents
    )


def create_kit_definition(*, key, label, **context):
    """Create a kit definition with no contents yet; its key is permanent (KDEF-1, IMP-1)."""
    org = context["organization_id"]
    key, label = _key(key), _label(label)

    def build(actor):
        if KitDefinition.objects.filter(organization_id=org, key=key).exists():
            raise KitDefinitionError(f"kit definition key {key!r} is already in use")
        definition_id = new_id()

        def apply(receipt, _current):
            KitDefinition.objects.create(
                id=definition_id, organization_id=org, key=key, label=label, active=True
            )

        body = {"code": "KIT_DEFINITION_CREATED", "definition_id": definition_id, "key": key}
        return _plan(org, body, apply, definition_id=definition_id, key=key, label=label)

    return _run(context, CREATE_COMMAND, {"key": key, "label": label}, build)


def save_kit_definition_draft(*, definition_id, contents, **context):
    """Insert the next draft revision of a definition with its complete contents (KDEF-2)."""
    canonical_id(definition_id)
    org = context["organization_id"]

    def build(actor):
        definition = _locked_definition(org, definition_id)
        if not definition.active:
            raise KitDefinitionError("kit definition is inactive")
        validated = validate_contents(org, contents)
        top = KitDefinitionVersion.objects.filter(definition=definition).aggregate(
            m=Max("revision")
        )
        revision, version_id = (top["m"] or 0) + 1, new_id()

        def apply(receipt, _current):
            _insert_version(org, definition, revision, version_id, validated, receipt.recorded_at)

        body = {
            "code": "KIT_DEFINITION_DRAFT_SAVED",
            "version_id": version_id,
            "revision": revision,
            "content_count": len(validated),
        }
        return _plan(org, body, apply, definition_id=definition_id, **body)

    request = {"definition_id": definition_id, "contents": contents}
    return _run(context, DRAFT_COMMAND, request, build)


def publish_kit_definition_version(*, version_id, effective_at=None, **context):
    """Publish a draft at effective_at (None: when recorded), never earlier (KDEF-2, CHK-3)."""
    canonical_id(version_id)
    org = context["organization_id"]
    requested = _aware(effective_at)

    def build(actor):
        version = KitDefinitionVersion.objects.filter(organization_id=org, pk=version_id).first()
        if version is None:
            raise KitDefinitionError(f"kit definition version {version_id} does not exist")
        definition = _locked_definition(org, version.definition_id)
        if not definition.active:
            raise KitDefinitionError("kit definition is inactive")
        if _is_published(version_id):
            raise KitDefinitionError("kit definition version is already published")
        publication_id = new_id()

        def apply(receipt, current):
            final = requested or receipt.recorded_at
            if final < receipt.recorded_at:
                raise KitDefinitionError("effective_at must not be earlier than the publication")
            latest = (
                KitDefinitionPublication.objects.filter(version__definition=definition)
                .order_by("-effective_at")
                .values_list("effective_at", flat=True)
                .first()
            )
            if latest is not None and final <= latest:
                raise KitDefinitionError(
                    f"effective_at must be later than the latest published effective instant {latest}"
                )
            KitDefinitionPublication.objects.create(
                id=publication_id,
                organization_id=org,
                version=version,
                effective_at=final,
                command=receipt,
                recorded_at=receipt.recorded_at,
                actor_user=current.user,
                actor_roles_at_time=list(current.roles),
            )

        body = {
            "code": "KIT_DEFINITION_PUBLISHED",
            "publication_id": publication_id,
            "version_id": version_id,
        }
        return _plan(org, body, apply, definition_id=str(definition.pk), **body)

    request = {
        "version_id": version_id,
        "effective_at": requested.isoformat() if requested else None,
    }
    return _run(context, PUBLISH_COMMAND, request, build)


def duplicate_kit_definition(*, source_version_id, key, label, **context):
    """A new definition whose first draft copies one version's contents, without history."""
    canonical_id(source_version_id)
    org = context["organization_id"]
    key, label = _key(key), _label(label)

    def build(actor):
        source = KitDefinitionVersion.objects.filter(
            organization_id=org, pk=source_version_id
        ).first()
        if source is None:
            raise KitDefinitionError(f"kit definition version {source_version_id} does not exist")
        if KitDefinition.objects.filter(organization_id=org, key=key).exists():
            raise KitDefinitionError(f"kit definition key {key!r} is already in use")
        copied = [
            {
                "item_id": row.item_id,
                "display_order": row.display_order,
                "kind": row.kind,
                "instructions": row.instructions,
                "expected_quantity": row.expected_quantity,
                "unit": row.unit,
                "allowed_outcomes": list(row.allowed_outcomes),
                "expiration_tracked": row.expiration_tracked,
            }
            for row in KitDefinitionItem.objects.filter(version=source).order_by("display_order")
        ]
        if not copied:
            raise KitDefinitionError("the source version has no contents")
        definition_id, version_id = new_id(), new_id()

        def apply(receipt, _current):
            definition = KitDefinition.objects.create(
                id=definition_id, organization_id=org, key=key, label=label, active=True
            )
            _insert_version(org, definition, 1, version_id, copied, receipt.recorded_at)

        body = {
            "code": "KIT_DEFINITION_DUPLICATED",
            "definition_id": definition_id,
            "version_id": version_id,
            "content_count": len(copied),
        }
        return _plan(org, body, apply, source_version_id=source_version_id, key=key, **body)

    request = {"source_version_id": source_version_id, "key": key, "label": label}
    return _run(context, DUPLICATE_COMMAND, request, build)


def set_kit_definition_active(*, definition_id, active, **context):
    """Deactivate or reactivate a definition; kit instances that have it keep it (KDEF-2)."""
    canonical_id(definition_id)
    if type(active) is not bool:
        raise KitDefinitionError("active must be true or false")
    org = context["organization_id"]

    def build(actor):
        definition = _locked_definition(org, definition_id)
        if definition.active is active:
            raise KitDefinitionError(
                f"kit definition is already {'active' if active else 'inactive'}"
            )

        def apply(receipt, _current):
            KitDefinition.objects.filter(pk=definition_id).update(active=active)

        body = {
            "code": "KIT_DEFINITION_ACTIVE_SET",
            "definition_id": definition_id,
            "active": active,
        }
        return _plan(org, body, apply, **body)

    return _run(context, ACTIVE_COMMAND, {"definition_id": definition_id, "active": active}, build)


def version_effective_at(organization_id, definition_id, at):
    """The published version of a definition in force at instant `at`, or None (KDEF-4).

    The version with the latest effective instant at or before `at`; publications of one
    definition are strictly ordered by effective instant, so the answer is unique.
    """
    publication = (
        KitDefinitionPublication.objects.filter(
            organization_id=organization_id,
            version__definition_id=definition_id,
            effective_at__lte=at,
        )
        .order_by("-effective_at")
        .first()
    )
    return publication.version if publication else None
