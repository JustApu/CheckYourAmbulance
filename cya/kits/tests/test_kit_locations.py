"""Find and move physical kits from one organization-wide list, over HTTP (issue #277)."""

import html
import re
import uuid
from unittest import mock

from django.test import override_settings

from cya.accounts.models import Organization
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.kits.commands import KitError, add_kit_instance
from cya.kits.models import KitAssignmentEvent
from cya.kits.positions import revise_position
from cya.kits.tests.test_kit_admin_views import KitAdminCase
from cya.testing.role_http import real_collaborator_settings

URL = "/administration/kit-instances/locations/"


@override_settings(**real_collaborator_settings())
class KitLocationTests(KitAdminCase):
    def setUp(self):
        super().setUp()
        self.client_, _ = self.harness.sign_in("officer")  # a Supply Officer may move kits
        self.on_a = self.add_typed("Synthetic kit on A", self.vehicle)
        self.on_b = self.add_typed("Synthetic kit on B", self.vehicle_b)
        self.in_stock = self.add_typed("Synthetic stock kit", None)

    def add_typed(self, label, vehicle):
        placement = (
            {"vehicle_id": str(vehicle.pk), "compartment_id": str(self.compartment_of(vehicle).pk)}
            if vehicle is not None
            else {}
        )
        return add_kit_instance(
            label=label,
            definition_id=self.definition,
            **placement,
            **self.context(self.supervisor),
        ).body["kit_id"]

    def latest(self, kit_id):
        return KitAssignmentEvent.objects.filter(kit_id=kit_id).order_by("-sequence").first()

    def target(self, vehicle):
        return f"{vehicle.pk}:{self.compartment_of(vehicle).pk}"

    def save(self, kit_id, target, *, expected=None, client=None, **fields):
        expected = expected or str(self.latest(kit_id).pk)
        return self.post(
            client or self.client_,
            URL,
            stage="save",
            kit_id=kit_id,
            expected_event=expected,
            target=target,
            **fields,
        )

    def labels(self, response):
        return re.findall(r"<h3>([^<]+)</h3>", response.content.decode())

    # Listing and search

    def test_every_active_kit_is_listed_with_type_and_location(self):
        retired = self.add_typed("Synthetic retired kit", None)
        self.deactivate(retired)
        page = self.client_.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            self.labels(page), ["Synthetic kit on A", "Synthetic kit on B", "Synthetic stock kit"]
        )
        body = page.content.decode()
        self.assertIn("Kit type: Synthetic jump kit", body)
        self.assertIn(f"Current location: {self.vehicle.display_name}", html.unescape(body))
        self.assertIn("Current location: Unassigned stock", body)
        self.assertIn("3 active physical kits.", body)

    def test_search_matches_label_kit_type_vehicle_and_compartment(self):
        cases = {
            "stock kit": ["Synthetic stock kit"],
            "jump": ["Synthetic kit on A", "Synthetic kit on B", "Synthetic stock kit"],
            self.vehicle_b.display_name: ["Synthetic kit on B"],
            self.cabinet.label: [
                "Synthetic kit on A"
            ],  # each vehicle's compartment has its own label
            "cabinet": ["Synthetic kit on A", "Synthetic kit on B"],
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                page = self.client_.get(URL, {"q": query})
                self.assertEqual(self.labels(page), expected)
                self.assertContains(page, f"{len(expected)} matching physical kit")

    def test_location_filter_narrows_to_stock_or_one_vehicle(self):
        stock = self.client_.get(URL, {"location": "stock"})
        self.assertEqual(self.labels(stock), ["Synthetic stock kit"])
        on_b = self.client_.get(URL, {"location": str(self.vehicle_b.pk)})
        self.assertEqual(self.labels(on_b), ["Synthetic kit on B"])

    def test_an_invalid_filter_lists_nothing(self):
        for params in ({"location": "nowhere"}, {"q": "x" * 101}):
            with self.subTest(params=params):
                page = self.client_.get(URL, params)
                self.assertEqual(page.status_code, 400)
                self.assertContains(page, "The kit list is hidden", status_code=400)
                self.assertEqual(self.labels(page), [])

    def test_a_long_list_is_paged(self):
        for number in range(25):
            self.add_typed(f"Synthetic extra kit {number:02d}", None)
        first = self.client_.get(URL)
        self.assertEqual(len(self.labels(first)), 25)
        self.assertContains(first, "page 1 of 2")
        second = self.client_.get(URL, {"page": "2"})
        self.assertEqual(len(self.labels(second)), 3)
        self.assertContains(second, "Previous page")

    def test_a_renamed_compartment_shows_and_matches_its_current_name(self):
        revise_position(
            kind="COMPARTMENT",
            vehicle_id=str(self.vehicle.pk),
            stable_slot_id=str(self.cabinet.stable_slot_id),
            label="Synthetic renamed shelf",
            **self.context(self.supervisor),
        )
        page = self.client_.get(URL, {"q": "renamed shelf"})
        self.assertEqual(self.labels(page), ["Synthetic kit on A"])
        self.assertContains(page, "Synthetic renamed shelf")
        # The kit still points at the older revision row; choosing its slot again is no move.
        live = Compartment.objects.filter(
            vehicle=self.vehicle, stable_slot_id=self.cabinet.stable_slot_id
        ).order_by("-revision")[0]
        before = KitAssignmentEvent.objects.count()
        response = self.save(self.on_a, f"{self.vehicle.pk}:{live.pk}")
        self.assertContains(response, "Nothing to save", status_code=400)
        self.assertEqual(KitAssignmentEvent.objects.count(), before)

    # Moving

    def test_a_direct_move_between_vehicles_is_one_event(self):
        before = self.latest(self.on_a)
        with mock.patch("cya.kits.commands.reconcile_vehicle") as reconcile:
            reconcile.return_value.failures = []
            response = self.save(self.on_a, self.target(self.vehicle_b), q="Synthetic")
        self.assertEqual(response.status_code, 302)
        self.assertIn("q=Synthetic", response["Location"])
        self.assertTrue(response["Location"].endswith(f"#kit-{self.on_a}"))
        events = list(KitAssignmentEvent.objects.filter(kit_id=self.on_a).order_by("sequence"))
        self.assertEqual(len(events), 2)  # no stock step between the vehicles
        moved = events[-1]
        self.assertEqual(moved.prior_event_id, before.pk)
        self.assertEqual(
            (moved.vehicle_id, moved.compartment_id), (self.vehicle_b.pk, self.cabinet_b.pk)
        )
        kit = KitInstance.objects.get(pk=self.on_a)
        self.assertEqual(kit.vehicle_id, self.vehicle_b.pk)
        # Both vehicles' expectations are reconciled, the old one and the new one.
        self.assertEqual(
            sorted(call.args[1] for call in reconcile.call_args_list),
            sorted([str(self.vehicle.pk), str(self.vehicle_b.pk)]),
        )
        confirmed = self.client_.get(response["Location"].split("#")[0])
        self.assertContains(
            confirmed,
            f"Saved: Synthetic kit on A is now on {self.vehicle_b.display_name}",
        )

    def test_stock_to_vehicle_and_back(self):
        to_vehicle = self.save(self.in_stock, self.target(self.vehicle))
        self.assertEqual(to_vehicle.status_code, 302)
        self.assertEqual(KitInstance.objects.get(pk=self.in_stock).vehicle_id, self.vehicle.pk)
        to_stock = self.save(self.in_stock, "stock")
        self.assertEqual(to_stock.status_code, 302)
        self.assertIsNone(KitInstance.objects.get(pk=self.in_stock).vehicle_id)
        confirmed = self.client_.get(to_stock["Location"].split("#")[0])
        self.assertContains(confirmed, "Saved: Synthetic stock kit is now in unassigned stock.")

    def test_a_stale_row_is_refused_without_any_change(self):
        shown = str(self.latest(self.on_a).pk)
        self.move(self.on_a, self.vehicle_b)  # someone else moves it after the page loaded
        after = KitAssignmentEvent.objects.count()
        response = self.save(self.on_a, "stock", expected=shown)
        self.assertContains(response, "this kit was moved or changed", status_code=409)
        self.assertEqual(KitAssignmentEvent.objects.count(), after)
        self.assertEqual(KitInstance.objects.get(pk=self.on_a).vehicle_id, self.vehicle_b.pk)
        # The row now shows the kit's current place rather than the refused choice.
        body = response.content.decode()
        self.assertRegex(body, rf'value="{self.target(self.vehicle_b)}" selected')

    def test_choosing_the_current_location_saves_nothing(self):
        before = KitAssignmentEvent.objects.count()
        response = self.save(self.on_a, self.target(self.vehicle))
        self.assertContains(response, "Nothing to save", status_code=400)
        self.assertEqual(KitAssignmentEvent.objects.count(), before)

    def test_a_location_not_offered_is_refused(self):
        inactive = Vehicle.objects.create(
            organization=self.org, display_name="Synthetic inactive", active=False
        )
        before = KitAssignmentEvent.objects.count()
        for target in ("", "nowhere", f"{inactive.pk}:{self.cabinet.pk}"):
            with self.subTest(target=target):
                response = self.save(self.on_a, target)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(KitAssignmentEvent.objects.count(), before)

    def test_a_refused_rule_keeps_the_pending_choice(self):
        # A rule the command enforces (here a stand-in refusal) keeps the row's choice.
        with mock.patch(
            "cya.kits.commands._placement", side_effect=KitError("compartment is retired")
        ):
            response = self.save(self.on_a, self.target(self.vehicle_b))
        self.assertContains(response, "Not saved: compartment is retired.", status_code=400)
        self.assertRegex(
            response.content.decode(), rf'value="{self.target(self.vehicle_b)}" selected'
        )

    def test_a_replayed_form_moves_once(self):
        operation_id = str(uuid.uuid4())
        expected = str(self.latest(self.on_a).pk)
        first = self.save(
            self.on_a, self.target(self.vehicle_b), expected=expected, operation_id=operation_id
        )
        again = self.save(
            self.on_a, self.target(self.vehicle_b), expected=expected, operation_id=operation_id
        )
        self.assertEqual((first.status_code, again.status_code), (302, 302))
        self.assertEqual(KitAssignmentEvent.objects.filter(kit_id=self.on_a).count(), 2)

    def test_a_reused_operation_id_for_another_change_is_refused(self):
        operation_id = str(uuid.uuid4())
        self.save(self.on_a, "stock", operation_id=operation_id)
        response = self.save(self.on_b, "stock", operation_id=operation_id)
        self.assertContains(response, "already used for a different change", status_code=409)
        self.assertEqual(KitInstance.objects.get(pk=self.on_b).vehicle_id, self.vehicle_b.pk)

    # Scope and permission

    def test_crew_is_refused(self):
        client, _ = self.harness.sign_in("crew")
        self.assertEqual(client.get(URL).status_code, 403)
        response = self.save(self.on_a, "stock", client=client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(KitAssignmentEvent.objects.filter(kit_id=self.on_a).count(), 1)

    def test_another_organizations_kit_is_not_found(self):
        other = Organization.objects.create(name="Synthetic other org", station_zone="UTC")
        foreign = KitInstance.objects.create(organization=other, label="Synthetic foreign kit")
        response = self.save(str(foreign.pk), "stock", expected=str(uuid.uuid4()))
        self.assertContains(response, "not available", status_code=404)
        self.assertNotContains(response, "Synthetic foreign kit", status_code=404)

    def test_malformed_posts_change_nothing(self):
        before = KitAssignmentEvent.objects.count()
        for fields in (
            {"kit_id": "not-a-uuid"},
            {"expected_event": "not-a-uuid"},
            {"operation_id": "not-a-uuid"},
        ):
            with self.subTest(fields=fields):
                data = {
                    "stage": "save",
                    "kit_id": self.on_a,
                    "expected_event": str(self.latest(self.on_a).pk),
                    "target": "stock",
                    **fields,
                }
                response = self.post(self.client_, URL, **data)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(self.client_, URL, stage="other").status_code, 400)
        self.assertEqual(KitAssignmentEvent.objects.count(), before)
