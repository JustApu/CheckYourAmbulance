"""Seeded, bounded synthetic histories through real audited command interfaces; no network."""

from datetime import time, timedelta
from random import Random

from cya.operations.commands import set_vehicle_status
from cya.operations.status import current_status


def _kit_action(case, rng, kit_ids):
    """Add a kit instance, bring one back, or take one away (SPEC 1.8.0 / 1.10.0)."""
    from cya.checklists.models import KitInstance

    # The first kit is the one the checklist names to mark the slot; publishing requires it to be
    # active (checklist validation), so only the others come and go.
    movable = kit_ids[1:]
    gone = [k for k in movable if not KitInstance.objects.get(pk=k).active]
    present = [k for k in movable if k not in gone]
    roll = rng.random()
    if gone and roll < 0.4:
        case.move(rng.choice(gone), case.vehicle)
    elif len(kit_ids) < 4 and (roll < 0.7 or not present):
        kit_ids.append(case.add_kit(f"Synthetic kit {len(kit_ids)}").body["kit_id"])
    elif present:
        case.deactivate(rng.choice(present))


def generate_history(case, seed, days=14, kits=False, checks=False):
    """Yield observation instants over weeks with publications, schedule/status changes and jobs.

    With kits=True the case must be a KitCase: the checklist gains an item inside a kit and the
    history also adds, removes and returns kit instances.
    """
    if not 14 <= days <= 28:
        raise ValueError("synthetic history must span 14 to 28 days")
    rng = Random(seed)
    kit_ids = []
    if kits:
        kit_ids.append(case.add_kit("Synthetic reference kit").body["kit_id"])
        case.items = [*case.base_items(), case.spec(case.kit_item, 80, kit_id=kit_ids[0])]

    def publish():
        return case.published_version(case.items) if kits else case.published_version()

    kinds = ["status", "publish", "schedule", *(["kit"] if kits else [])]
    version_id = publish()
    category = _category(case) if checks else None
    case.publish_schedule("test-0600")
    case.bind(reconcile=False)
    for day in range(days):
        case.clock.set(case.at(days=day, hours=1))
        # Periodic events guarantee each category; seeded choices vary their payload/order.
        actions = list(kinds) if day % 3 == 0 else [rng.choice(kinds)]
        rng.shuffle(actions)
        for action in actions:
            if action == "status":
                previous = current_status(str(case.org.pk), str(case.vehicle.pk))
                status = (
                    "IN_SERVICE"
                    if previous and previous.status == "OUT_OF_SERVICE"
                    else "OUT_OF_SERVICE"
                )
                set_vehicle_status(
                    vehicle_id=str(case.vehicle.pk),
                    status=status,
                    explanation="Synthetic generated history.",
                    **case.context(case.supervisor),
                )
            elif action == "kit":
                if not checks:
                    _kit_action(case, rng, kit_ids)
            elif action == "publish":
                version_id = publish()
            else:
                case.publish_schedule(
                    "test-0600",
                    wall_time=time(6, rng.choice([0, 30])),
                    effective_at=case.clock.now,
                    early_seconds=rng.choice([1800, 3600, 7200]),
                )
        # Some missed wakeups followed by later runs exercise the recovery interval.
        if rng.random() > 0.2:
            summary = case.generate()
            if summary.failures:
                raise AssertionError(f"synthetic generator failed for seed {seed}, day {day}")
        if checks:
            case.clock.set(case.at(days=day, hours=5, minutes=45))
            if "kit" in actions:
                # Exercise TIME-1a while the inspection window is already open.
                _kit_action(case, rng, kit_ids)
                yield case.clock.now
            yield from _check_actions(case, rng, day, version_id, category, kit_ids)
        case.clock.set(case.at(days=day, hours=12))
        summary = case.generate()
        if summary.failures:
            raise AssertionError(f"synthetic recovery failed for seed {seed}, day {day}")
        yield case.clock.now


def _category(case):
    from cya.accounts.fields import new_id
    from cya.deficiencies.models import DeficiencyCategory
    from cya.deficiencies.severity import ensure_default_severities

    ensure_default_severities(case.org.pk, case.clock_start - timedelta(days=1))
    return DeficiencyCategory.objects.create(
        organization=case.org,
        stable_category_id=new_id(),
        revision=1,
        label="Synthetic replay category",
        supply_related=False,
        effective_at=case.clock_start - timedelta(days=1),
    )


def _check_actions(case, rng, day, version_id, category, kit_ids):
    from cya.checks.commands import correct_check, invalidate_chain, record_check
    from cya.conditions.uti import complete_unable_obligation
    from cya.kits.history import active_kits
    from cya.obligations.queries import effective_obligations

    def record(item, outcome, actor=None, **extra):
        from cya.checks.conflicts import CheckConflict

        args = dict(
            vehicle_id=str(case.vehicle.pk),
            item_id=str(item.pk),
            checklist_version_id=version_id,
            outcome=outcome,
            purpose="ROUTINE",
            **extra,
        )
        try:
            result = record_check(**args, **case.context(actor or case.crew))
        except CheckConflict as conflict:
            # One explicit proceed decision with a new operation id; no concurrent writer is
            # present in this deterministic history, so a second warning is a test failure.
            args["acknowledges_check_id"] = conflict.newest["check_id"]
            result = record_check(**args, **case.context(actor or case.crew))
        return result.body["check_id"]

    fields = {
        "create": {
            "category_id": str(category.stable_category_id),
            "severity": "HIGH",
            "summary": "Synthetic replay deficiency",
            "description": "Synthetic inspection evidence.",
        }
    }
    if day % 3 == 2:
        root = record(
            case.suction,
            "UNABLE_TO_INSPECT",
            actor=case.supervisor,
            explanation="Synthetic access restriction.",
            deficiency=fields,
        )
        yield case.clock.now
        candidates = effective_obligations(
            str(case.org.pk),
            case.at(days=day),
            case.at(days=day + 1),
            vehicle_id=str(case.vehicle.pk),
            as_of=case.clock.now,
        )
        for obligation in candidates:
            if obligation.item_id == case.suction.pk and obligation.early_at <= case.clock.now:
                complete_unable_obligation(
                    obligation_id=str(obligation.pk),
                    check_id=root,
                    explanation="Synthetic supervisor review.",
                    **case.context(case.supervisor),
                )
                break
        yield case.clock.now
        invalidate_chain(
            check_id=root,
            explanation="Synthetic Unable chain invalidation.",
            **case.context(case.admin),
        )
        yield case.clock.now
    else:
        if day % 6 == 0:
            record(case.suction, "PASSED", actor=case.supervisor)
            yield case.clock.now
        root = record(case.suction, "FAILED", deficiency=fields)
        yield case.clock.now
        if day % 3 == 0:
            # Independent Pass/Fail commits preserve the failure's unresolved deficiency.
            record(case.suction, "PASSED", actor=case.supervisor, basis_check_id=root)
            yield case.clock.now
        corrected = correct_check(
            check_id=root,
            expected_tip_check_id=root,
            outcome="PASSED",
            explanation="Synthetic entry correction.",
            **case.context(case.crew),
        ).body["check_id"]
        yield case.clock.now
        invalidate_chain(
            check_id=corrected,
            explanation="Synthetic chain invalidation.",
            **case.context(case.admin),
        )
        yield case.clock.now
    if kit_ids:
        present = active_kits(str(case.org.pk), str(case.vehicle.pk), case.slot, case.clock.now)
        if present:
            kit = rng.choice(present)
            record(case.kit_item, "PASSED", kit_id=kit.kit_id)
            yield case.clock.now
