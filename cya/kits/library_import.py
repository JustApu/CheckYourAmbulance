"""Kit-library import and export (SPEC 4.7 IMP-1 to IMP-4, issue #129).

preview_kit_library reads only: it parses the file (cya.kits.library_files), resolves every
kit and item, and reports per kit whether it would be created, changed or left unchanged, the
items it would create, and every problem. Its base token summarises the configuration the
preview was computed from.

import_kit_library is one audited command (policy action import_configuration, Administrators
only; SPEC 3.3) that recomputes the same plan under row locks, refuses it if the base token no
longer matches (IMP-3: a stale preview is refused, not merged) or if the plan has problems, and
otherwise writes everything or nothing: new items, new definitions, a new draft revision for each
changed kit, and changed labels. It never publishes (KDEF-2) and never deactivates or deletes
anything the file omits (IMP-2). Re-importing identical data changes nothing. The audit keeps
counts only, never file contents (IMP-4).

Items are matched by exact label: exactly one active item with that label is used; a label
with no item creates one; a label shared by several active items, or held only by inactive
items, is refused, naming the label.

export_kit_library writes each active definition's latest revision (draft or published), so an
export re-imports unchanged.
"""

import hashlib
from dataclasses import dataclass, field

from django.db.models import Max

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id, load_state
from cya.authorization.guard import PermissionDenied, Principal, require_permission
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import DatabaseRoleBackend, digest
from cya.authorization.policy import Role
from cya.checklists.models import Item
from cya.core.audit_commands import Actor, CommandDenied, Plan, run_command
from cya.kits import library_files
from cya.kits.definitions import (
    MAX_LABEL,
    KitDefinitionError,
    check_key,
    check_label,
    content_rules,
)
from cya.kits.models import KitDefinition, KitDefinitionItem, KitDefinitionVersion

IMPORT_ACTION = "import_configuration"
IMPORT_COMMAND = "KIT_LIBRARY_IMPORT"
CREATE, CHANGE, UNCHANGED = "CREATE", "CHANGE", "UNCHANGED"


class ImportRefused(ValueError):
    """The import was not applied; .errors lists why. The command is audited as failed."""

    def __init__(self, errors):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


@dataclass
class KitPlan:
    key: str
    label: str
    action: str
    definition_id: str | None
    label_changed: bool
    contents: list  # validated entries, each with "item" (label) and its rule fields


@dataclass
class LibraryPlan:
    kits: list = field(default_factory=list)
    new_items: list = field(default_factory=list)  # labels, in first-seen order
    errors: list = field(default_factory=list)
    base_token: str = ""

    def summary(self):
        counts = {CREATE: 0, CHANGE: 0, UNCHANGED: 0}
        for kit in self.kits:
            counts[kit.action] += 1
        return {
            "created": counts[CREATE],
            "changed": counts[CHANGE],
            "unchanged": counts[UNCHANGED],
            "new_items": len(self.new_items),
        }


# --- reading the current configuration -------------------------------------------------------


def _latest_version(definition):
    top = KitDefinitionVersion.objects.filter(definition=definition).aggregate(m=Max("revision"))
    if top["m"] is None:
        return None
    return KitDefinitionVersion.objects.get(definition=definition, revision=top["m"])


def _version_contents(version, labels_by_id):
    if version is None:
        return []
    rows = KitDefinitionItem.objects.filter(version=version).order_by("display_order", "id")
    return [
        {
            "item": labels_by_id[str(row.item_id)],
            "kind": row.kind,
            "expected_quantity": row.expected_quantity,
            "unit": row.unit,
            "allowed_outcomes": list(row.allowed_outcomes),
            "expiration_tracked": row.expiration_tracked,
            "instructions": row.instructions,
        }
        for row in rows
    ]


def _item_labels(organization_id):
    return {
        str(pk): label
        for pk, label in Item.objects.filter(organization_id=organization_id).values_list(
            "pk", "label"
        )
    }


def _active_items_by_label(organization_id, labels):
    found = {}
    for pk, label, active in Item.objects.filter(
        organization_id=organization_id, label__in=labels
    ).values_list("pk", "label", "active"):
        found.setdefault(label, []).append((str(pk), active))
    return found


# --- planning --------------------------------------------------------------------------------


def _check_kit(index, kit, errors):
    where = f"kits[{index}]"
    try:
        check_key(kit["key"])
        check_label(kit["label"])
    except KitDefinitionError as error:
        errors.append(f"{where} ({kit.get('key')!r}): {error}")
        return None
    entries = []
    for position, entry in enumerate(kit["contents"]):
        if entry is None:
            continue  # already reported by the file parser
        spot = f"{where} ({kit['key']}) contents[{position}]"
        item = entry.get("item")
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_LABEL:
            errors.append(
                f"{spot}: item must be a nonblank label of at most {MAX_LABEL} characters"
            )
            continue
        try:
            entries.append({"item": item, **content_rules(entry, spot)})
        except KitDefinitionError as error:
            errors.append(str(error))
    seen = [e["item"] for e in entries]
    for label in sorted({label for label in seen if seen.count(label) > 1}):
        errors.append(f"{where} ({kit['key']}): item {label!r} appears more than once")
    return entries


def _resolve_items(organization_id, labels, errors):
    """{label: item id or None (to be created)}; ambiguous or inactive-only labels are errors."""
    by_label = _active_items_by_label(organization_id, labels)
    resolved = {}
    for label in labels:
        rows = by_label.get(label, [])
        active = [pk for pk, is_active in rows if is_active]
        if len(active) == 1:
            resolved[label] = active[0]
        elif len(active) > 1:
            errors.append(
                f"item {label!r} matches {len(active)} active items; make the labels unique first"
            )
        elif rows:
            errors.append(
                f"item {label!r} exists only as an inactive item; reactivate or rename it first"
            )
        else:
            resolved[label] = None
    return resolved


def build_plan(organization_id, kits, lock=False, source=""):
    """The plan for parsed kits against the current configuration (no writes).

    `source` is the uploaded file's digest, so the base token also binds the confirm to the
    exact file that was previewed.
    """
    plan, keys = LibraryPlan(), [k.get("key") for k in kits]
    for key in sorted({k for k in keys if keys.count(k) > 1 and isinstance(k, str)}):
        plan.errors.append(f"kit key {key!r} appears more than once in the file")
    checked = [(kit, _check_kit(i, kit, plan.errors)) for i, kit in enumerate(kits)]
    labels = list(dict.fromkeys(e["item"] for _, entries in checked if entries for e in entries))
    resolved = _resolve_items(organization_id, labels, plan.errors)
    plan.new_items = [label for label in labels if resolved.get(label, "") is None]
    definitions = KitDefinition.objects.filter(
        organization_id=organization_id, key__in=[k for k in keys if isinstance(k, str)]
    ).order_by("key")
    if lock:
        definitions = definitions.select_for_update()
    by_key = {d.key: d for d in definitions}
    labels_by_id = _item_labels(organization_id)
    kit_state = []
    for kit, entries in checked:
        if entries is None:
            continue
        current = by_key.get(kit["key"])
        latest = _latest_version(current) if current else None
        kit_state.append(
            [
                kit["key"],
                str(current.pk) if current else None,
                str(latest.pk) if latest else None,
                current.label if current else None,
                current.active if current else None,
            ]
        )
        if current is not None and not current.active:
            plan.errors.append(
                f"kit {kit['key']!r} is inactive; reactivate it before importing changes"
            )
            continue
        same = current is not None and _version_contents(latest, labels_by_id) == entries
        action = CREATE if current is None else (UNCHANGED if same else CHANGE)
        plan.kits.append(
            KitPlan(
                key=kit["key"],
                label=kit["label"],
                action=action,
                definition_id=str(current.pk) if current else None,
                label_changed=current is not None and current.label != kit["label"],
                contents=entries,
            )
        )
    # What the plan was computed from: each named kit's definition, latest revision, label and
    # active flag, and how each item label resolved. Any change makes a confirm stale (IMP-3).
    item_state = sorted([label, resolved.get(label, "unresolved")] for label in labels)
    plan.base_token = digest({"kits": sorted(kit_state), "items": item_state, "source": source})
    return plan


def preview_kit_library(organization_id, data, fmt):
    """Parse and plan an uploaded kit library without writing (IMP-2)."""
    try:
        kits = library_files.parse(data, fmt)
    except library_files.FileErrors as error:
        return LibraryPlan(errors=list(error.errors))
    return build_plan(organization_id, kits, source=digest_bytes(data))


# --- the import command ----------------------------------------------------------------------


def _authorize(organization_id, actor_id, action):
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
                require_permission(principal, organization_id, action)
            except PermissionDenied:
                raise CommandDenied(actor) from None
            return actor

    return authorize


def _apply(organization_id, plan, recorded_at):
    item_ids = {}
    for label in plan.new_items:
        item_ids[label] = new_id()
        Item.objects.create(
            id=item_ids[label], organization_id=organization_id, label=label, active=True
        )
    for label, pk in _active_items_by_label(
        organization_id,
        [e["item"] for kit in plan.kits for e in kit.contents if e["item"] not in item_ids],
    ).items():
        item_ids[label] = next(p for p, active in pk if active)
    for kit in plan.kits:
        if kit.action == UNCHANGED and not kit.label_changed:
            continue
        if kit.action == CREATE:
            definition = KitDefinition.objects.create(
                id=new_id(),
                organization_id=organization_id,
                key=kit.key,
                label=kit.label,
                active=True,
            )
        else:
            definition = KitDefinition.objects.get(pk=kit.definition_id)
            if kit.label_changed:
                KitDefinition.objects.filter(pk=definition.pk).update(label=kit.label)
        if kit.action == UNCHANGED:
            continue
        top = KitDefinitionVersion.objects.filter(definition=definition).aggregate(
            m=Max("revision")
        )
        version = KitDefinitionVersion.objects.create(
            id=new_id(),
            organization_id=organization_id,
            definition=definition,
            revision=(top["m"] or 0) + 1,
            created_at=recorded_at,
        )
        KitDefinitionItem.objects.bulk_create(
            KitDefinitionItem(
                id=new_id(),
                organization_id=organization_id,
                version=version,
                item_id=item_ids[entry["item"]],
                display_order=position + 1,
                **{k: v for k, v in entry.items() if k != "item"},
            )
            for position, entry in enumerate(kit.contents)
        )


def import_kit_library(*, data, fmt, base_token, **context):
    """Apply a previewed kit library, all or nothing, as drafts (IMP-3)."""
    org = context["organization_id"]
    canonical_id(context["actor_id"])
    if not isinstance(base_token, str) or len(base_token) != 64:
        raise ImportRefused(["base_token: preview the file first and confirm that preview"])
    try:
        kits = library_files.parse(data, fmt)
    except library_files.FileErrors as error:
        raise ImportRefused(list(error.errors)) from None

    def build(actor):
        plan = build_plan(org, kits, lock=True, source=digest_bytes(data))
        if plan.errors:
            raise ImportRefused(plan.errors)
        if plan.base_token != base_token:
            # The library, an item label's resolution, or the file itself differs from the preview.
            raise ImportRefused(
                ["the kit library changed since this file was previewed; preview it again"]
            )

        def apply(receipt, _current):
            _apply(org, plan, receipt.recorded_at)

        body = {"code": "KIT_LIBRARY_IMPORTED", **plan.summary()}
        return Plan(
            response_body=body,
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={
                "schema_version": 1,
                "values": {"file_type": library_files.FILE_TYPE, "format": fmt, **plan.summary()},
            },
        )

    return run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=IMPORT_COMMAND,
        request_digest=digest({"fmt": fmt, "data": digest_bytes(data), "base_token": base_token}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, context["actor_id"], IMPORT_ACTION),
        prepare=build,
    )


def digest_bytes(data):
    return hashlib.sha256(data if isinstance(data, bytes) else b"").hexdigest()


# --- export ----------------------------------------------------------------------------------


def library_kits(organization_id, keys=None):
    """The active definitions' latest revisions in normal form, by key (IMP-4)."""
    definitions = KitDefinition.objects.filter(organization_id=organization_id, active=True)
    if keys is not None:
        definitions = definitions.filter(key__in=list(keys))
    labels_by_id = _item_labels(organization_id)
    kits = []
    for definition in definitions.order_by("key"):
        contents = _version_contents(_latest_version(definition), labels_by_id)
        if contents:
            kits.append({"key": definition.key, "label": definition.label, "contents": contents})
    return kits


def export_kit_library(organization_id, fmt, keys=None):
    """The kit library as a file; an export re-imports unchanged (IMP-4)."""
    return library_files.render(library_kits(organization_id, keys), fmt)


EXPORT_ACTION = "export_configuration"
EXPORT_COMMAND = "KIT_LIBRARY_EXPORT"


def audited_export(*, fmt, **context):
    """Export the kit library as an audited, write-free command (IMP-4, RPT-2).

    Returns (result, file bytes). Supervisors and Administrators may export (SPEC 3.3); the
    audit records the actor, format and kit count, never the contents.
    """
    org = context["organization_id"]
    canonical_id(context["actor_id"])
    if fmt not in library_files.FORMATS:
        raise ImportRefused([f"format must be one of {list(library_files.FORMATS)}"])
    produced = {}

    def build(actor):
        kits_out = library_kits(org)
        produced["data"] = library_files.render(kits_out, fmt)
        kits = len(kits_out)

        def apply(receipt, _current):
            return None

        values = {"file_type": library_files.FILE_TYPE, "format": fmt, "kits": kits}
        return Plan(
            response_body={"code": "KIT_LIBRARY_EXPORTED", **values},
            apply=apply,
            target_type="ORGANIZATION",
            target_id=org,
            new_snapshot={"schema_version": 1, "values": values},
        )

    result = run_command(
        organization_id=org,
        operation_id=context["operation_id"],
        command_type=EXPORT_COMMAND,
        request_digest=digest({"fmt": fmt}),
        correlation_id=context["correlation_id"],
        request_attempt_id=context["request_attempt_id"],
        authorize=_authorize(org, context["actor_id"], EXPORT_ACTION),
        prepare=build,
    )
    return result, produced.get("data")
