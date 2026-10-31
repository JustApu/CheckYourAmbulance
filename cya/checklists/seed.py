"""Synthetic sample EMS organization fleet and checklist seed (SPEC CHK-7, OQ-01).

Reads tests/fixtures/demo-fleet.json (marked synthetic and
non-authoritative). Fleet rows (vehicles, slots, items, template) get
deterministic ids derived from the fixture namespace. Fixture keys are stable
identity inputs; changing display names does not change them. Seeding is idempotent;
the checklist version is created and published through the audited commands as a
named administrator, so it has the same receipt, audit and freeze evidence as a
real publication. The fixture asserts no stocking requirement.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4, uuid5

from django.db import transaction
from django.utils import timezone

from cya.checklists.commands import publish_template_version, save_template_draft
from cya.checklists.models import (
    ChecklistPublication,
    ChecklistTemplate,
    Compartment,
    Item,
    Vehicle,
    VehicleSystem,
)
from cya.testing.fixtures import load_fixture

FIXTURE = "demo-fleet.json"


@dataclass(frozen=True)
class SeedPlan:
    vehicles: int
    slots: int
    items: int
    template_id: str
    publishes: bool


def _load():
    data = load_fixture(FIXTURE)
    if data.get("non_authoritative") is not True:
        raise ValueError(f"{FIXTURE} must be marked non_authoritative")
    return data


class _Ids:
    def __init__(self, namespace):
        self.namespace = UUID(namespace)

    def __call__(self, *parts):
        return str(uuid5(self.namespace, ":".join(parts)))


def plan_seed(organization_id):
    """Read-only preview of what seed_fleet would create."""
    data = _load()
    ids = _Ids(data["namespace"])
    template_id = ids("template", data["template"]["key"])
    published = ChecklistPublication.objects.filter(
        organization_id=organization_id, checklist_version__template_id=template_id
    ).exists()
    return SeedPlan(
        vehicles=len(data["vehicles"]),
        slots=len(data["vehicles"]) * (len(data["compartments"]) + len(data["systems"])),
        items=len(data["items"]),
        template_id=template_id,
        publishes=not published,
    )


def _create_missing(model, organization_id, rows):
    existing = set(
        model.objects.filter(pk__in=[r["id"] for r in rows]).values_list("pk", flat=True)
    )
    model.objects.bulk_create(
        model(organization_id=organization_id, **row) for row in rows if row["id"] not in existing
    )


def _seed_rows(organization_id, data, ids, now):
    _create_missing(
        Vehicle,
        organization_id,
        [
            {
                "id": ids("vehicle", v["key"]),
                "display_name": v["display_name"],
                "number": v["number"],
            }
            for v in data["vehicles"]
        ],
    )
    for model, kind, key in (
        (Compartment, "compartment", "compartments"),
        (VehicleSystem, "system", "systems"),
    ):
        _create_missing(
            model,
            organization_id,
            [
                {
                    "id": ids(kind, v["key"], s["key"], "1"),
                    "vehicle_id": ids("vehicle", v["key"]),
                    "stable_slot_id": ids("slot", kind, s["key"]),
                    "revision": 1,
                    "label": s["label"],
                    "effective_at": now,
                }
                for v in data["vehicles"]
                for s in data[key]
            ],
        )
    _create_missing(
        Item,
        organization_id,
        [{"id": ids("item", i["key"]), "label": i["label"]} for i in data["items"]],
    )
    template = data["template"]
    _create_missing(
        ChecklistTemplate,
        organization_id,
        [{"id": ids("template", template["key"]), "label": template["label"]}],
    )


def _definitions(data, ids):
    """Definitions reference the first vehicle's slot rows; other vehicles map by slot id."""
    reference_vehicle = data["vehicles"][0]["key"]
    definitions = []
    for d in data["template"]["definitions"]:
        optional = [k for k in ("expected_quantity", "unit") if k in d]
        spec = {
            k: d[k]
            for k in ("display_order", "kind", "instructions", "allowed_outcomes", *optional)
        }
        spec["item_id"] = ids("item", d["item"])
        if "compartment" in d:
            spec["compartment_id"] = ids("compartment", reference_vehicle, d["compartment"], "1")
        else:
            spec["system_id"] = ids("system", reference_vehicle, d["system"], "1")
        definitions.append(spec)
    return definitions


def seed_fleet(*, organization_id, actor_id):
    """Create the synthetic fleet and publish its checklist. Safe to run again."""
    plan = plan_seed(organization_id)
    data = _load()
    ids = _Ids(data["namespace"])
    now = timezone.now()
    with transaction.atomic():
        _seed_rows(organization_id, data, ids, now)
    if not plan.publishes:
        return plan, None
    context = {"organization_id": organization_id, "actor_id": actor_id}
    draft = save_template_draft(
        template_id=plan.template_id,
        items=_definitions(data, ids),
        operation_id=str(uuid4()),
        correlation_id=str(uuid4()),
        request_attempt_id=str(uuid4()),
        **context,
    )
    publication = publish_template_version(
        version_id=draft.body["version_id"],
        operation_id=str(uuid4()),
        correlation_id=str(uuid4()),
        request_attempt_id=str(uuid4()),
        **context,
    )
    return plan, publication
