"""Vehicle crew assignment (SPEC 15.4 ASG-1 to ASG-8, AC-39, issue #202)."""

from datetime import timedelta

from django.db import DatabaseError, transaction

from cya.accounts.fields import new_id
from cya.accounts.models import User
from cya.checklists.models import Vehicle
from cya.core.audit_commands import CommandDenied
from cya.obligations.tests.support import ObligationCase
from cya.operations.assignments import (
    AssignmentError,
    assign_vehicle,
    assignment_history,
    assignment_limit_hours,
    current_assignment,
    current_crew,
    end_assignment,
    extend_assignment,
    set_assignment_limit,
)
from cya.operations.models import VehicleAssignmentEvent
from cya.operations.vehicles import create_vehicle, set_vehicle_active

VIN = "1FDXE4FS8KDC00031"  # synthetic


def chain(person):
    return list(
        VehicleAssignmentEvent.objects.filter(person=person)
        .order_by("sequence")
        .values_list("action", "source")
    )


class AssignmentCase(ObligationCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.vehicles
        self.partner = self.make_user("partner")
        self.other = self.make_user("other")
        self.supply = self.make_user("supply")
        self.grant(self.supply, "SUPPLY_OFFICER")
        self.clock.set(self.at(hours=7))

    def hours(self, n):
        return self.clock.now + timedelta(hours=n)

    def assign(self, person, vehicle, hours=12, actor=None):
        return assign_vehicle(
            person_id=str(person.pk),
            vehicle_id=str(vehicle.pk),
            ends_at=self.hours(hours),
            **self.context(actor or person),
        ).body

    def current(self, person):
        return current_assignment(str(self.org.pk), str(person.pk))


class OwnAssignmentTests(AssignmentCase):
    def test_every_role_assigns_and_moves_itself(self):
        for person in (self.crew, self.supervisor, self.supply, self.admin):
            with self.subTest(person=person.idp_subject):
                body = self.assign(person, self.a)
                self.assertEqual((body["code"], body["source"]), ("ASSIGN", "SELF"))
                self.assertEqual(str(self.current(person).vehicle_id), str(self.a.pk))
                body = self.assign(person, self.b)
                self.assertEqual((body["code"], body["from_vehicle_id"]), ("MOVE", str(self.a.pk)))
                self.assertEqual(str(self.current(person).vehicle_id), str(self.b.pk))

    def test_assigning_to_the_same_vehicle_is_refused(self):
        self.assign(self.crew, self.a)
        with self.assertRaises(AssignmentError) as caught:
            self.assign(self.crew, self.a)
        self.assertEqual(caught.exception.code, "ALREADY_ASSIGNED")

    def test_end_time_must_be_ahead_within_the_limit_and_zoned(self):
        for hours, code in ((-1, "END_IN_PAST"), (0, "END_IN_PAST"), (24.5, "END_TOO_LATE")):
            with self.subTest(hours=hours), self.assertRaises(AssignmentError) as caught:
                self.assign(self.crew, self.a, hours=hours)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(AssignmentError) as caught:
            assign_vehicle(
                person_id=str(self.crew.pk),
                vehicle_id=str(self.a.pk),
                ends_at="2026-06-01T19:00:00",
                **self.context(self.crew),
            )
        self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")
        self.assign(self.crew, self.a, hours=24)
        self.assertEqual(chain(self.crew), [("ASSIGN", "SELF")])

    def test_an_assignment_expires_without_an_event_and_is_never_renewed(self):
        self.assign(self.crew, self.a, hours=11)
        self.clock.set(self.hours(11))
        self.assertIsNone(self.current(self.crew))
        self.assertEqual(current_crew(str(self.org.pk), str(self.a.pk)), [])
        for command in (
            lambda: extend_assignment(
                person_id=str(self.crew.pk), ends_at=self.hours(2), **self.context(self.crew)
            ),
            lambda: end_assignment(person_id=str(self.crew.pk), **self.context(self.crew)),
        ):
            with self.assertRaises(AssignmentError) as caught:
                command()
            self.assertEqual(caught.exception.code, "NOT_ASSIGNED")
        # Assigning again after the end time is a new assignment, not a renewal (ASG-5).
        self.assertEqual(self.assign(self.crew, self.a)["code"], "ASSIGN")
        self.assertEqual(chain(self.crew), [("ASSIGN", "SELF"), ("ASSIGN", "SELF")])

    def test_extend_sets_a_later_end_time_within_the_limit_from_now(self):
        self.assign(self.crew, self.a, hours=2)
        with self.assertRaises(AssignmentError) as caught:
            extend_assignment(
                person_id=str(self.crew.pk), ends_at=self.hours(1), **self.context(self.crew)
            )
        self.assertEqual(caught.exception.code, "NOT_LATER")
        self.clock.set(self.hours(1.5))
        target = self.hours(24)
        extend_assignment(person_id=str(self.crew.pk), ends_at=target, **self.context(self.crew))
        self.assertEqual(self.current(self.crew).ends_at, target)
        with self.assertRaises(AssignmentError) as caught:
            extend_assignment(
                person_id=str(self.crew.pk), ends_at=self.hours(25), **self.context(self.crew)
            )
        self.assertEqual(caught.exception.code, "END_TOO_LATE")

    def test_ending_stops_it_now(self):
        self.assign(self.crew, self.a)
        body = end_assignment(person_id=str(self.crew.pk), **self.context(self.crew)).body
        self.assertEqual((body["code"], body["source"]), ("END", "SELF"))
        self.assertIsNone(self.current(self.crew))
        event = VehicleAssignmentEvent.objects.get(person=self.crew, action="END")
        self.assertEqual((event.end_reason, event.ends_at), ("ENDED", None))


class CrewCorrectionTests(AssignmentCase):
    """ASG-2(b): anyone currently on a vehicle may correct its crew list."""

    def test_a_partner_on_the_vehicle_extends_and_ends_anothers_assignment(self):
        self.assign(self.crew, self.a)
        self.assign(self.partner, self.a)
        body = extend_assignment(
            person_id=str(self.crew.pk), ends_at=self.hours(20), **self.context(self.partner)
        ).body
        self.assertEqual(body["source"], "CREW")
        end_assignment(person_id=str(self.crew.pk), **self.context(self.partner))
        self.assertEqual(
            chain(self.crew), [("ASSIGN", "SELF"), ("EXTEND", "CREW"), ("END", "CREW")]
        )

    def test_a_partner_moves_someone_over_from_the_wrong_vehicle(self):
        self.assign(self.crew, self.b)  # wrong truck
        self.assign(self.partner, self.a)
        body = self.assign(self.crew, self.a, actor=self.partner)
        self.assertEqual(
            (body["code"], body["source"], body["from_vehicle_id"]),
            ("MOVE", "CREW", str(self.b.pk)),
        )
        self.assertEqual(str(self.current(self.crew).vehicle_id), str(self.a.pk))

    def test_a_partner_adds_an_unassigned_person(self):
        self.assign(self.partner, self.a)
        self.assertEqual(self.assign(self.crew, self.a, actor=self.partner)["source"], "CREW")

    def test_someone_not_on_the_vehicle_cannot_change_its_crew(self):
        self.assign(self.crew, self.a)
        self.assign(self.partner, self.b)
        attempts = (
            lambda: extend_assignment(
                person_id=str(self.crew.pk), ends_at=self.hours(20), **self.context(self.partner)
            ),
            lambda: end_assignment(person_id=str(self.crew.pk), **self.context(self.partner)),
            # Moving a person onto a vehicle you are not on.
            lambda: self.assign(self.crew, self.b, actor=self.other),
            # Adding someone to a vehicle needs you to be on that vehicle, not theirs.
            lambda: self.assign(self.other, self.a, actor=self.partner),
        )
        for attempt in attempts:
            with self.assertRaises(CommandDenied):
                attempt()
        self.assertEqual(chain(self.crew), [("ASSIGN", "SELF")])
        self.assertIsNone(self.current(self.other))

    def test_supply_officers_follow_the_crew_rule(self):
        self.assign(self.crew, self.a)
        with self.assertRaises(CommandDenied):
            end_assignment(person_id=str(self.crew.pk), **self.context(self.supply))
        self.assign(self.supply, self.a)
        end_assignment(person_id=str(self.crew.pk), **self.context(self.supply))


class SupervisorTests(AssignmentCase):
    def test_supervisors_and_administrators_change_anyones_assignment(self):
        for actor in (self.supervisor, self.admin):
            with self.subTest(actor=actor.idp_subject):
                body = self.assign(self.crew, self.a, actor=actor)
                self.assertEqual(body["source"], "SUPERVISOR")
                self.assign(self.crew, self.b, actor=actor)
                extend_assignment(
                    person_id=str(self.crew.pk), ends_at=self.hours(20), **self.context(actor)
                )
                end_assignment(person_id=str(self.crew.pk), **self.context(actor))
                self.assertIsNone(self.current(self.crew))

    def test_any_number_of_people_on_one_vehicle_and_crew_read_back(self):
        people = (self.crew, self.partner, self.other, self.supply)
        for n, person in enumerate(people):
            self.assign(person, self.a, hours=4 + n)
        crew = current_crew(str(self.org.pk), str(self.a.pk))
        self.assertEqual([str(c.person_id) for c in crew], [str(p.pk) for p in people])
        self.assertEqual(current_crew(str(self.org.pk), str(self.b.pk)), [])

    def test_history_lists_moves_on_both_vehicles(self):
        self.assign(self.crew, self.a)
        self.assign(self.crew, self.b)
        on_a = assignment_history(str(self.org.pk), vehicle_id=str(self.a.pk))
        self.assertEqual([e.action for e in on_a], ["MOVE", "ASSIGN"])


class InactivePersonTests(AssignmentCase):
    def test_a_deactivated_person_leaves_the_crew_list_and_can_still_be_ended(self):
        self.assign(self.crew, self.a)
        User.objects.filter(pk=self.crew.pk).update(active=False)
        self.assertEqual(current_crew(str(self.org.pk), str(self.a.pk)), [])
        with self.assertRaises(AssignmentError) as caught:
            extend_assignment(
                person_id=str(self.crew.pk), ends_at=self.hours(20), **self.context(self.supervisor)
            )
        self.assertEqual(caught.exception.code, "PERSON_INACTIVE")
        end_assignment(person_id=str(self.crew.pk), **self.context(self.supervisor))
        self.assertEqual(chain(self.crew)[-1], ("END", "SUPERVISOR"))


class InactiveVehicleTests(AssignmentCase):
    def test_deactivating_ends_the_crew_and_inactive_vehicles_cannot_be_assigned(self):
        vehicle_id = create_vehicle(
            vin=VIN, display_name="Synthetic Unit 31", number="31", **self.context(self.supervisor)
        ).body["vehicle_id"]
        vehicle = Vehicle.objects.get(pk=vehicle_id)
        self.assign(self.crew, vehicle)
        self.assign(self.supervisor, vehicle)
        self.assign(self.partner, self.a)
        set_vehicle_active(vehicle_id=vehicle_id, active=False, **self.context(self.supervisor))
        self.assertEqual(current_crew(str(self.org.pk), vehicle_id), [])
        self.assertEqual(chain(self.crew)[-1], ("END", "SUPERVISOR"))
        self.assertEqual(chain(self.supervisor)[-1], ("END", "SELF"))
        self.assertEqual(
            set(
                VehicleAssignmentEvent.objects.filter(action="END").values_list(
                    "end_reason", flat=True
                )
            ),
            {"VEHICLE_INACTIVE"},
        )
        self.assertIsNotNone(self.current(self.partner))  # other vehicles are untouched

        with self.assertRaises(AssignmentError) as caught:
            self.assign(self.crew, vehicle)
        self.assertEqual(caught.exception.code, "VEHICLE_INACTIVE")

    def test_deactivating_also_ends_a_deactivated_users_assignment(self):
        """PR #216 review: hidden from the crew list, but still ended with the vehicle (ASG-3)."""
        vehicle_id = create_vehicle(
            vin=VIN, display_name="Synthetic Unit 31", number="31", **self.context(self.supervisor)
        ).body["vehicle_id"]
        self.assign(self.crew, Vehicle.objects.get(pk=vehicle_id))
        User.objects.filter(pk=self.crew.pk).update(active=False)
        set_vehicle_active(vehicle_id=vehicle_id, active=False, **self.context(self.supervisor))
        self.assertEqual(chain(self.crew)[-1], ("END", "SUPERVISOR"))
        self.assertIsNone(self.current(self.crew))
        self.assertEqual(current_crew(str(self.org.pk), vehicle_id, include_inactive=True), [])


class LimitTests(AssignmentCase):
    def test_an_administrator_sets_the_maximum(self):
        self.assertEqual(assignment_limit_hours(str(self.org.pk)), 24)
        for actor in (self.crew, self.supervisor):
            with self.assertRaises(CommandDenied):
                set_assignment_limit(max_hours=48, **self.context(actor))
        set_assignment_limit(max_hours=48, **self.context(self.admin))
        self.assertEqual(assignment_limit_hours(str(self.org.pk)), 48)
        self.assign(self.crew, self.a, hours=47)
        with self.assertRaises(AssignmentError):
            set_assignment_limit(max_hours=48, **self.context(self.admin))
        for bad in (0, 169, 12.5, "24"):
            with self.subTest(bad=bad), self.assertRaises(AssignmentError):
                set_assignment_limit(max_hours=bad, **self.context(self.admin))


class GuardTests(AssignmentCase):
    """The database refuses rows that break the chain, whatever writes them."""

    def copy(self, event, **changes):
        fields = {
            "id": new_id(),
            "organization_id": event.organization_id,
            "person_id": event.person_id,
            "sequence": event.sequence + 1,
            "prior_event_id": event.pk,
            "action": "ASSIGN",
            "vehicle_id": event.vehicle_id,
            "ends_at": event.ends_at,
            "source": event.source,
            "command_id": event.command_id,
            "recorded_at": event.recorded_at,
            "actor_user_id": event.actor_user_id,
            "actor_roles_at_time": event.actor_roles_at_time,
        }
        fields.update(changes)
        with transaction.atomic():
            VehicleAssignmentEvent.objects.create(**fields)

    def test_rows_that_break_the_rules_are_refused(self):
        self.assign(self.crew, self.a)
        first = VehicleAssignmentEvent.objects.get(person=self.crew)
        attempts = {
            "second current assignment": {},
            "move from the wrong vehicle": {"action": "MOVE", "from_vehicle_id": self.b.pk},
            "self source by someone else": {
                "person_id": self.partner.pk,
                "sequence": 1,
                "prior_event_id": None,
            },
        }
        for name, changes in attempts.items():
            with self.subTest(name), self.assertRaises(DatabaseError):
                self.copy(first, **changes)
        with self.assertRaises(DatabaseError), transaction.atomic():
            VehicleAssignmentEvent.objects.filter(pk=first.pk).update(source="CREW")
        with self.assertRaises(DatabaseError), transaction.atomic():
            VehicleAssignmentEvent.objects.filter(pk=first.pk).delete()

    def test_crew_rebuilt_from_history_matches_the_live_read(self):
        """AC-39 (6): replaying every chain gives the same current crew."""
        self.assign(self.crew, self.a, hours=3)
        self.assign(self.partner, self.a)
        self.assign(self.other, self.b)
        self.assign(self.crew, self.b, actor=self.supervisor)
        end_assignment(person_id=str(self.other.pk), **self.context(self.other))
        self.clock.set(self.hours(4))
        now = self.clock.now
        replayed = {}
        for event in VehicleAssignmentEvent.objects.order_by("person_id", "sequence"):
            replayed[event.person_id] = event
        expected = sorted(
            (str(e.vehicle_id), str(e.person_id))
            for e in replayed.values()
            if e.action != "END" and e.ends_at > now
        )
        live = sorted((str(c.vehicle_id), str(c.person_id)) for c in current_crew(str(self.org.pk)))
        self.assertEqual(live, expected)
        self.assertEqual(
            set(live), {(str(self.a.pk), str(self.partner.pk)), (str(self.b.pk), str(self.crew.pk))}
        )
