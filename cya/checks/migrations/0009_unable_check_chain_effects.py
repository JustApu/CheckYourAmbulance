"""Tie UTI-3 child determinations to their atomic check correction/invalidation."""

from importlib import import_module

from django.db import migrations

prior = import_module("cya.checks.migrations.0008_determination_check")


def trigger_sql():
    previous = prior.trigger_sql()[1]
    sql = previous.replace(
        "'SUPERVISOR_COMPLETION_INVALIDATION'\n ))",
        "'SUPERVISOR_COMPLETION_INVALIDATION', 'CHECK_CORRECTION', 'CHECK_INVALIDATION'\n ))",
    )
    if sql == previous:
        raise RuntimeError("determination receipt command list changed")
    sql = sql.replace(
        "p.determination_type='INVALIDATED'",
        """(p.determination_type='INVALIDATED' OR (p.determination_type='SUPERSEDED'
 AND EXISTS (SELECT 1 FROM obligation_determination origin
  WHERE origin.id=p.prior_determination_id AND origin.organization_id=p.organization_id
   AND origin.obligation_id=p.obligation_id AND origin.determination_type='SUPERVISOR_COMPLETED')))""",
    )
    sql = sql.replace(
        "IF NEW.determination_type='INVALIDATED' AND (",
        """IF NEW.determination_type='INVALIDATED' AND NOT EXISTS (
 SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
  AND r.command_type='CHECK_INVALIDATION') AND (""",
    )
    return (
        sql[:-3]
        + """IF EXISTS (SELECT 1 FROM command_receipt r WHERE r.id=NEW.command_id
 AND r.command_type IN ('CHECK_CORRECTION','CHECK_INVALIDATION')) AND (
 NEW.explanation IS NULL OR TRIM(NEW.explanation)=''
 OR NOT EXISTS (
 SELECT 1 FROM obligation_determination p
 JOIN determination_check dc ON dc.determination_id=p.id
 JOIN check_record c ON c.id=dc.check_id
 JOIN check_event e ON e.root_check_id=c.root_check_id
 JOIN command_receipt r ON r.id=NEW.command_id
 WHERE p.id=NEW.prior_determination_id AND p.organization_id=NEW.organization_id
  AND p.obligation_id=NEW.obligation_id AND p.determination_type='SUPERVISOR_COMPLETED'
  AND p.vehicle_id <=> NEW.vehicle_id AND p.due_at <=> NEW.due_at
  AND p.recorded_at<=NEW.recorded_at
  AND dc.organization_id=NEW.organization_id AND c.organization_id=NEW.organization_id
  AND c.outcome='UNABLE_TO_INSPECT' AND e.organization_id=NEW.organization_id
  AND e.id=JSON_UNQUOTE(JSON_EXTRACT(NEW.evaluated_inputs,'$.check_event_id'))
  AND c.root_check_id=JSON_UNQUOTE(JSON_EXTRACT(NEW.evaluated_inputs,'$.root_check_id'))
  AND e.command_id=NEW.command_id AND e.recorded_at=NEW.recorded_at
  AND e.actor_user_id=NEW.actor_user_id AND e.actor_roles_at_time=NEW.actor_roles_at_time
  AND NOT EXISTS (SELECT 1 FROM obligation_determination child
   WHERE child.prior_determination_id=p.id)
  AND ((r.command_type='CHECK_INVALIDATION' AND e.event_type='INVALIDATION'
    AND NEW.determination_type='INVALIDATED')
   OR (r.command_type='CHECK_CORRECTION' AND e.event_type='CORRECTION'
    AND NEW.determination_type='SUPERSEDED' AND EXISTS (
     SELECT 1 FROM check_record replacement WHERE replacement.id=e.replacement_check_id
      AND replacement.outcome IN ('PASSED','FAILED'))))))
THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='completion check event mismatch'; END IF;
END"""
    )


def reverse_guard(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM obligation_determination d JOIN command_receipt r"
            " ON r.id=d.command_id WHERE r.command_type IN ('CHECK_CORRECTION','CHECK_INVALIDATION')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("cannot reverse while check-chain determination effects exist")


class Migration(migrations.Migration):
    dependencies = [("cya_checks", "0008_determination_check")]
    operations = [
        migrations.RunSQL(
            ["DROP TRIGGER cya_obligation_determination_bi", trigger_sql()],
            reverse_sql=["DROP TRIGGER cya_obligation_determination_bi", prior.trigger_sql()[1]],
        ),
        migrations.RunPython(migrations.RunPython.noop, reverse_guard),
    ]
