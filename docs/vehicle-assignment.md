# Vehicle crew assignment

Who is working on which vehicle, and until when. The rules are in SPEC §15.4 (ASG-1 to ASG-8). The owner decided them on [issue #202](https://github.com/JustApu/CheckYourAmbulance/issues/202) and approved them as SPEC 4.4.0. This page covers the backend: the data, the commands, who may use them, and how to read the current crew. The screens and notices come in later parts of #202.

An assignment never restricts who may open an inspection, record a check or report a problem. It is not the roster that conditional obligations use (ASG-6). Nothing here reads or writes checks, sessions or obligation determinations.

## Data

`vehicle_assignment_event` (append-only, `cya.operations.models.VehicleAssignmentEvent`) keeps one linear chain of events per person. The rule it protects is per person: one vehicle at a time (ASG-3).

| Action | Meaning |
|---|---|
| `ASSIGN` | Starts an assignment. Allowed only when the person has no current assignment. |
| `MOVE` | Puts a person who is on one vehicle onto another, in one event. `from_vehicle` names the vehicle they left. |
| `EXTEND` | Sets a later end time on the current assignment. |
| `END` | Stops the current assignment early. `end_reason` is `ENDED` when a person ends it, or `VEHICLE_INACTIVE` when the vehicle was deactivated. |

- **Current assignment.** A person's newest event says where they are. Nowhere, if it is `END` or its `ends_at` has passed.
- **Expiry.** An assignment reaching its end time writes no event (ASG-4), and nothing renews it (ASG-5).
- **Source.** `SELF` when the actor is the person, `CREW` for a correction by someone on the vehicle, `SUPERVISOR` for a Supervisor or Administrator acting on someone else. `ROSTER` arrives with the Humanity adapter (ASG-1).

`vehicle_assignment_limit` (append-only) holds revisions of the longest an assignment or an extension may run. The latest revision applies, and without one the limit is 24 hours (ASG-4, OQ-79). An Administrator sets it, from 1 to 168 hours.

Migration `0007_vehicle_assignment_guards` installs the database guards, so a row written outside the commands is refused. They cover:
- receipt attribution and which command type may write each action;
- `SELF` exactly when the actor is the person;
- the chain;
- `ASSIGN` only with no current assignment, and `MOVE`, `EXTEND` and `END` only on a current one;
- `MOVE` must leave the vehicle the person was on;
- `EXTEND` only to a later end time;
- an active vehicle for every action except `END`.

The table rows and command types are in the [data-model contracts](data-model-contracts.md).

## Commands (`cya.operations.assignments`)

Each command runs through `run_command`, which handles the receipt, replay and audit. Do not wrap a command in a transaction.

| Command | Command type | Effect |
|---|---|---|
| `assign_vehicle(person_id, vehicle_id, ends_at)` | `VEHICLE_ASSIGN` | `ASSIGN`, or `MOVE` when the person is on another vehicle. Refused (`ALREADY_ASSIGNED`) when they are already on this one: extend instead. |
| `extend_assignment(person_id, ends_at)` | `VEHICLE_ASSIGNMENT_EXTEND` | `EXTEND`. The new end must be later than the current one (`NOT_LATER`). |
| `end_assignment(person_id)` | `VEHICLE_ASSIGNMENT_END` | `END`, reason `ENDED`. |
| `set_assignment_limit(max_hours)` | `VEHICLE_ASSIGNMENT_LIMIT_SET` | A new limit revision. Administrator only, through `configure_schedules_thresholds_notifications`. |

**`ends_at`** is an aware `datetime` or an ISO 8601 instant with an offset. It is stored in UTC. It must be ahead (`END_IN_PAST`) and no more than the limit after the moment of the command (`END_TOO_LATE`). That applies to an extension too, so a holdover can be extended again and again, but never more than the limit ahead.

**Deactivation.** Deactivating a vehicle (`set_vehicle_active(active=False)`) ends everyone currently assigned to it, inside the same command, with reason `VEHICLE_INACTIVE`. Assigning, moving or extending onto an inactive vehicle is refused (`VEHICLE_INACTIVE`).

Other errors raise `AssignmentError` with `code`, `status` and `path`:
- `NOT_ASSIGNED`: the person has no current assignment to extend or end;
- `PERSON_INACTIVE`: the person is not an active user with a role. Ending an assignment is still allowed, so a deactivated person's assignment can be cleared;
- `NOT_FOUND`;
- `MALFORMED_REQUEST`.

## Who may do what (ASG-2, SPEC §3.3)

The command reads the facts under the actor's and person's identity locks, then picks one policy row:

| Situation | Policy action | Rows | Source |
|---|---|---|---|
| The actor is the person | `assign_own_vehicle` (`own_only`, every role) | PERM-50 | `SELF` |
| The actor is a Supervisor or Administrator | `manage_vehicle_assignments` | PERM-52 | `SUPERVISOR` |
| Anyone else | `correct_vehicle_crew` (`on_vehicle_only`, every role) | PERM-51 | `CREW` |

**The target vehicle.** `on_vehicle_only` allows the change only when the actor is currently assigned to the target vehicle:
- for an assignment or move, the vehicle the person is going to. So a partner can move someone over from the wrong truck, or add an unassigned person, onto their own vehicle.
- for an extension or end, the person's current vehicle. So crew can extend or end assignments only on the vehicle they are on.

Anything else is denied and audited, and nothing changes. `view_current_crew` (PERM-54, every role) governs `current_crew` screens, and `view_vehicle_assignment_history` (PERM-53, Supervisor and Administrator) governs `assignment_history`.

## Reads

- `current_assignment(organization_id, person_id, at=None)`: the person's `Assignment` (event, vehicle, `ends_at`), or `None`.
- `current_crew(organization_id, vehicle_id=None, at=None)`: everyone currently assigned, for one vehicle or all, ordered by vehicle and then end time. Every active user with a role may see it (ASG-8, `view_current_crew`). It lists only active users. `include_inactive=True` keeps deactivated users, for rules that must reach every current assignment: deactivating a vehicle ends all of them. It is rebuilt on every read from the newest event of each chain and the clock, so it cannot disagree with the history (AC-39 (6)).
- `assignment_history(organization_id, person_id=None, vehicle_id=None)`: events newest first. A vehicle's history includes moves off it. Callers check `view_vehicle_assignment_history`.
- `assignment_limit_hours(organization_id)`: the current limit.

## Screens (`cya.operations.crew_views`, issue #202 part 2)

The four tabs are unchanged (owner, 2026-11-18), so crew lives where people already look. Every change is a plain form posted to `/status/crew/` (`vehicle-crew`), which runs the audited command and returns to the page it came from. Hiding a control is only a convenience: the command decides who may act.

- **Times.** Each form takes an "until" time of day in station time (`HH:MM`). The assignment ends the next time the station clock reaches it, so an overnight shift is entered as its morning end (`until_instant`). An optional station **date** beside the time sets an end beyond the next day, up to the limit (PR #219 review: with a time alone, a limit above 24 hours had no effect). The command still refuses a time more than the limit ahead. Clock changes (PR #219 review): in the autumn hour that repeats, the end is the earlier reading of that time that is still ahead, so it is never already past; a time the spring change skips is refused with "That time is skipped by the clock change tonight" rather than moved. Times show as "18:00", or "Thu 06:00" when not today.
- **Vehicles tab.**
  - The list shows each vehicle's crew ("Crew: You, Sam" or "No crew assigned"); every role may see it (`view_current_crew`).
  - A vehicle's page has a **Crew** section: who is on it until when, with Extend and End where the viewer may change that person, and "I'm on this truck", or "Move me here" when you are on another vehicle.
  - Anyone on the vehicle, and Supervisors and Administrators, also get **Add someone**. Its list shows every active user, and anyone on another vehicle is labelled "(on Medic 4 until 18:00, moves them)", never hidden (ASG-2).
  - Supervisors and Administrators see the last 20 events under **Crew history** (`view_vehicle_assignment_history`).
- **Home.** A **Your truck** card for every role:
  - with no assignment: "Which truck are you on?", with a truck and an until time;
  - with one: "You're on Medic 7 until 18:00", an "I'm off this truck" button, and, from 30 minutes before the end, "Still on Medic 7? Extend until ..." (ASG-5).
  - Your truck is listed first in Due today and in the supervisor vehicles table (ASG-7).
- **Administration.** A **Crew assignments** page under System (`/administration/crew-assignments/`), for Administrators, sets the limit in whole hours from 1 to 168.

## Still to come in #202

- **Notices** (part 3):
  - "the vehicle's current crew" as a recipient;
  - "your assignment was changed";
  - "a crew member was moved off your vehicle";
  - "check due on a vehicle".
