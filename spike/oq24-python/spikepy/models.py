"""THROWAWAY spike: unmanaged models over the spike schema."""
from django.db import models


class ItemLock(models.Model):
    pk = models.CompositePrimaryKey("vehicle_id", "item_id")
    vehicle_id = models.BigIntegerField()
    item_id = models.BigIntegerField()

    class Meta:
        managed = False
        db_table = "item_lock"
        app_label = "spikepy"


class CheckRecord(models.Model):
    check_id = models.BigAutoField(primary_key=True)
    operation_id = models.CharField(max_length=36, unique=True)
    vehicle_id = models.BigIntegerField()
    item_id = models.BigIntegerField()
    outcome = models.CharField(max_length=10)
    explanation = models.CharField(max_length=120, null=True)
    actor_user_id = models.BigIntegerField()
    basis_check_id = models.BigIntegerField(null=True)
    acknowledges_check_id = models.BigIntegerField(null=True)

    class Meta:
        managed = False
        db_table = "check_record"
        app_label = "spikepy"


class AuditEvent(models.Model):
    audit_id = models.BigAutoField(primary_key=True)
    actor_id = models.BigIntegerField(null=True)
    action = models.CharField(max_length=40)
    target = models.CharField(max_length=64, null=True)
    detail = models.CharField(max_length=200, null=True)

    class Meta:
        managed = False
        db_table = "audit_event"
        app_label = "spikepy"
