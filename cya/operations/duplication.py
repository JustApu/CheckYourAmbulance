"""Duplicating a checklist template or a vehicle (SPEC 4.8 CFG-3, CFG-4; issue #130 part 4).

Supervisors and Administrators (policy action duplicate_configuration, SPEC 3.3). The creator
enters the new record's key (SPEC 4.0.0 IMP-6), so both commands take the organization lock
first: keys are unique across the organization. See docs/configuration-management.md.
"""

from cya.accounts.fields import new_id
from cya.authorization.backend import canonical_id
from cya.checklists.models import ChecklistItemDef, ChecklistTemplate, ChecklistVersion
from cya.checklists.validation import validate_definitions
from cya.kits import keys as key_rules
from cya.kits.models import ChecklistKitPlacement
from cya.kits.placements import placements_of, validate_placements
from cya.operations.vehicles import MAX_NAME, VehicleError, _name, _plan, _run

ACTION = "duplicate_configuration"
TEMPLATE_COMMAND = "TEMPLATE_DUPLICATE"
# The item definition fields a draft is saved with (cya.checklists.validation.FIELDS).
DEFINITION_FIELDS = (
    "item_id",
    "display_order",
    "kind",
    "instructions",
    "expected_quantity",
    "unit",
    "allowed_outcomes",
    "expiration_tracked",
    "compartment_id",
    "system_id",
    "kit_id",
)

assert MAX_NAME >= 200  # a template label fits the same bound


class DuplicationError(VehicleError):
    """A duplication rule was violated; the command rolls back and is audited as failed."""


def _specs(version):
    """The source version's item definitions as draft specs, in display order."""
    specs = []
    for definition in ChecklistItemDef.objects.filter(checklist_version=version).order_by(
        "display_order", "id"
    ):
        spec = {name: getattr(definition, name) for name in DEFINITION_FIELDS}
        for name in ("item_id", "compartment_id", "system_id", "kit_id"):
            spec[name] = str(spec[name]) if spec[name] is not None else None
        spec["allowed_outcomes"] = list(spec["allowed_outcomes"])
        specs.append(spec)
    return specs


def duplicate_template(*, source_version_id, label, key, **context):
    """A new template with its creator's label and key, and one draft copying a version (CFG-3).

    The draft copies the chosen version's items, placements and settings through the same
    validation as save_template_draft. Nothing else is copied: no publication, override,
    binding, obligation or history. The source is unchanged.
    """
    canonical_id(source_version_id)
    name = _name(label)
    key = key_rules.check_key(key, "key")
    org = context["organization_id"]

    def build(actor):
        source = (
            ChecklistVersion.objects.filter(organization_id=org, pk=source_version_id)
            .select_related("template")
            .first()
        )
        if source is None:
            raise DuplicationError(f"checklist version {source_version_id} does not exist")
        if ChecklistTemplate.objects.filter(organization_id=org, key=key).exists():
            raise DuplicationError("another template has that key")
        definitions = validate_definitions(org, _specs(source))
        placed = validate_placements(
            org,
            [
                {"definition_id": str(p.definition_id), "display_order": p.display_order}
                for p in placements_of(source.pk)
            ],
        )
        template_id, version_id = new_id(), new_id()

        def apply(receipt, _current):
            ChecklistTemplate.objects.create(
                id=template_id, organization_id=org, label=name, active=True, key=key
            )
            ChecklistVersion.objects.create(
                id=version_id,
                organization_id=org,
                template_id=template_id,
                revision=1,
                created_at=receipt.recorded_at,
            )
            ChecklistItemDef.objects.bulk_create(
                ChecklistItemDef(
                    id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                )
                for fields in definitions
            )
            ChecklistKitPlacement.objects.bulk_create(
                ChecklistKitPlacement(
                    id=new_id(), organization_id=org, checklist_version_id=version_id, **fields
                )
                for fields in placed
            )

        body = {
            "code": TEMPLATE_COMMAND,
            "template_id": template_id,
            "version_id": version_id,
            "key": key,
            "label": name,
            "source_template_id": str(source.template_id),
            "source_version_id": source_version_id,
            "item_count": len(definitions),
            "placement_count": len(placed),
        }
        return _plan(org, body, apply)

    request = {"source_version_id": source_version_id, "label": name, "key": key}
    # Template keys are unique across the organization: take its lock first.
    return _run(context, TEMPLATE_COMMAND, request, build, lock_organization=True, action=ACTION)
