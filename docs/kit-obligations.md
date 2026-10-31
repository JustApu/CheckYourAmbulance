# Obligations per kit instance

[issue #92](https://github.com/JustApu/CheckYourAmbulance-archive/issues/92) implements [SPEC §4.2, §5.1, TIME-1 and TIME-1a, SEAL-10, §18 and §18.1](../SPEC.md) as
amended by SPEC 1.8.0 and 1.10.0 (decisions OQ-53 and OQ-54). A kit is a template; a vehicle keeps
zero, one or several instances of it; **each instance owes its own obligation**, so checking one
of several identical kits never satisfies the others. SPEC and the
[contracts](data-model-contracts.md) win if they disagree with this page.

## What exists

| Piece | Where |
|---|---|
| Append-only assignment history, guards, grants | `cya/kits/models.py`, migrations `0001_initial`, `0002_kit_guards` |
| "Which instances were in this slot on this vehicle, as the records stood" | `cya/kits/history.py` |
| Audited commands to add, move and deactivate a kit instance | `cya/kits/commands.py` |
| The kit instance in the obligation anchor | `cya/obligations/models.py`, migration `0003_kit_instance_anchor` |
| One requirement per instance | `cya/obligations/requirements.py` (`desired_at`) |
| Planning, writing, reading | `planning.py`, `generation.py`, `queries.py`, `projections.py`, `state.py` |
| Tests | `cya/kits/tests/`, `cya/obligations/tests/test_kits.py` |

## The assignment record

`kit_assignment_event` (append-only) records where a kit instance was and whether it was active:
a gapless per-kit sequence, each event strictly later than the one before, vehicle and
compartment (both set while active, both null once deactivated). An assignment takes effect when
it is recorded and is never backdated; the database refuses `effective_at` before `recorded_at`.
`kit_instance`'s vehicle, compartment and active flag are only a projection of the latest event,
updated in the same transaction. Every as-of question reads the events, never the row.

**Commands.** `add_kit_instance` creates the instance and its first event;
`record_kit_assignment` moves it (a vehicle and compartment) or deactivates it. Both need the
`manage_kit_assignment` permission (Supervisor, Supply Officer, Administrator; not Crew), run
through `run_command` (audited, idempotent on the operation id), and lock the vehicles involved
in id order before the kit, the same lock order as obligation generation. After the command
commits, those vehicles' upcoming obligations are reconciled; a failure there is logged and left
to the next `generate_obligations` run, as for requirement bindings. They are server functions;
there is no HTTP route or screen yet.

## Which instances a vehicle owes

An instance is in a slot on a vehicle at an instant when its latest event effective by then is
active, on that vehicle, in a compartment of that slot (`stable_slot_id`). A checklist item names
a slot through its kit reference; the slot is the one the reference kit was **first placed in**,
so moving or deactivating the reference kit does not change what the item means.

For a due instant `D` the generator writes one obligation per instance in the slot:

- no instance, no obligation; items outside a kit are unchanged;
- an instance added later is owed only for due instants whose early window had not yet opened at
  its activation (SPEC TIME-1a): an inspection whose window is already open is not required for
  it, and an inspection already due is not backfilled, so it is owed from the next due instant;
- an instance removed, deactivated or moved stops being owed: obligations due after the removal
  are retired (even in-window), earlier ones keep their state, so a removal cannot erase a miss;
- a move is a removal plus an addition; a temporary absence is a gap with no backfill;
- a kit that leaves and returns gets a new requirement (it pins the first event of its current
  run), so its retired obligations are never revived.

The assignment history is deliberately **not** cut off at the freeze instant that protects
in-window obligations from later checklist publications (SPEC §5.7): a removal is a physical fact
and retires what is due after it. Publications still behave as before, per instance.

## The anchor

An obligation, its requirement version and its selection stream gain `kit_instance` (the real
foreign key, null outside a kit) and `kit_key`: the same id, or the all-zero UUID for no kit, as
a NOT NULL value. MariaDB `UNIQUE` treats NULLs as distinct, so two rows differing only by a
missing instance would otherwise both be allowed; with `kit_key` they cannot (SPEC §18.1 item 3).
A CHECK keeps the two columns in step. The unique keys, the three composite anchor foreign keys
and the stream trigger now include `kit_key`, and a trigger refuses a requirement for a kit item
that names no instance, or pins an event that is not that instance being active on the vehicle in
the item's slot. A non-kit requirement's id is computed exactly as before, so no existing
obligation is replaced.

## Reading, and what check linking needs

Effective obligations, the as-of projection and the state engine carry `kit_instance_id`;
`effective_selection(..., kit_instance_id=)` names an instance. [issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74) links a check to the
obligation whose `kit_instance_id` equals the check's `kit_id` and to no other. State is derived
per obligation, so one instance's check cannot satisfy another's.

## Migration

`cya.kits` `0001` refuses (changing nothing) if any `kit_instance` row already exists: active kits
come only from the assignment history, an event needs a command receipt that cannot be invented
for a past action, and a kit with no history would silently owe nothing. Resolve such rows first.

`0003` adds columns with the all-zero default, so existing rows (all outside a kit) take it
from the default; no `UPDATE` runs, so the append-only triggers never fire. It drops the three anchor foreign keys and two triggers, replaces five unique keys and
recreates them against `kit_key`. **This migration refuses before changing its anchor** if any
row names a kit instance (a guard that runs first within this migration, because MariaDB DDL is
not transactional). Django may already have unapplied dependent migrations before reaching that
guard. The refusal test restores the migration leaves in a finally block and verifies the
check-record keys, indexes and append-only triggers are unchanged afterward; [issue #145](https://github.com/JustApu/CheckYourAmbulance-archive/issues/145) makes
that restoration work on dump-loaded worker databases too (see
[check-reference migration reversals](check-records.md#reversing-reference-migrations-on-cloned-test-databases)).
Without kit rows the migration reverses and re-applies cleanly, which
`cya.tests.test_audit_integrity_db` exercises for every migration.

## Not in this issue

Check-to-obligation linking and the conflict warning ([issue #74](https://github.com/JustApu/CheckYourAmbulance-archive/issues/74), [issue #75](https://github.com/JustApu/CheckYourAmbulance-archive/issues/75)); the attention state
([issue #83](https://github.com/JustApu/CheckYourAmbulance-archive/issues/83)); an HTTP route or screen to manage kits; per-instance expiration dates (the sealed-kit
workflow, SPEC §10.1); roles beyond `manage_kit_assignment`. SPEC §23 has no numbered criterion
for kit instances: `tests/scenarios/EX-kit-instance-obligations.json` is a pending contract
citing AC-1, because adding a numbered criterion needs a SPEC amendment.

## Verification

`python manage.py test cya.kits cya.obligations --noinput` covers: two identical kits owing
separately; none owing nothing; add, remove, return, move and temporary absence; publication per
instance; as-of history; idempotent regeneration; the database guards attacked directly; the
reverse-migration refusal; command authorization, validation, replay and a concurrent-move race;
and, with `CYA_DB_SECURITY_TESTS=1`, the real runtime account.
