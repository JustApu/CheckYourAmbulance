"""THROWAWAY spike: the check-recording algorithm through the Django ORM (same SPEC 7.3-7.4 semantics)."""
from django.db import OperationalError, transaction

from .models import AuditEvent, CheckRecord, ItemLock


def record_check(operation_id, vehicle_id, item_id, outcome, actor_user_id, explanation=None,
                 basis_check_id=None, acknowledges_check_id=None):
    for attempt in range(6):
        try:
            return _once(operation_id, vehicle_id, item_id, outcome, actor_user_id, explanation,
                         basis_check_id, acknowledges_check_id)
        except OperationalError as exc:
            if exc.args[0] != 1213 or attempt == 5:     # deadlock: rolled back by atomic(), safe to retry
                raise
    raise AssertionError("unreachable")


def _once(op, v, i, outcome, actor, explanation, basis, ack):
    with transaction.atomic():
        ItemLock.objects.select_for_update().get(vehicle_id=v, item_id=i)
        existing = CheckRecord.objects.filter(operation_id=op).values_list("check_id", flat=True).first()
        if existing is not None:
            return dict(status="REPLAY", check_id=existing)
        newest = (CheckRecord.objects.filter(vehicle_id=v, item_id=i).order_by("-check_id")
                  .values_list("check_id", "actor_user_id").first())
        if newest and newest[1] != actor:
            seen = ack if ack is not None else basis if basis is not None else -1
            if seen != newest[0]:
                transaction.set_rollback(True)          # a warning commits nothing
                return dict(status="CONFLICT", newest_id=newest[0])
        rec = CheckRecord.objects.create(operation_id=op, vehicle_id=v, item_id=i, outcome=outcome,
                                         explanation=explanation, actor_user_id=actor,
                                         basis_check_id=basis, acknowledges_check_id=ack)
        AuditEvent.objects.create(actor_id=actor, action="CHECK_RECORDED", target=f"check:{rec.check_id}", detail=f"op={op}")
        return dict(status="COMMITTED", check_id=rec.check_id)
