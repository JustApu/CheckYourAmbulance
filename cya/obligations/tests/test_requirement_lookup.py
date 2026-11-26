"""The batched newest-check lookup for item requirement keys (issue #278 Part G).

After the SPEC 6.0.0 item-requirement cutover every item outside a kit is keyed by its
requirement, so a checklist read made one query per item to find each key's newest check. The
lookup now makes one statement per chunk of keys. It must return exactly what the per-key query
returned, kept below as the reference, at every boundary and across a requirement's versions.
"""

from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from cya.checks.models import CheckRecord
from cya.deficiencies.tests.test_per_place import PerPlaceCase
from cya.obligations import projections


def reference_newest(organization_id, keys, lower):
    """The per-key ORM lookup the batched statement replaced."""
    units = projections._Units(keys)
    found = {}
    for vehicle_id, item_id, unit in sorted(k for k in keys if k[2] in units.requirements):
        newest = (
            CheckRecord.objects.filter(
                organization_id=organization_id,
                vehicle_id=vehicle_id,
                item_id=item_id,
                item_requirement_version_id__in=units.versions.get(unit, ()),
                recorded_at__lte=lower,
            )
            .order_by("-recorded_at")
            .values_list("recorded_at", flat=True)
            .first()
        )
        if newest is not None:
            found[(vehicle_id, item_id, unit)] = newest
    return found


class RequirementLookupTests(PerPlaceCase):
    def setUp(self):
        super().setUp()
        self.record_item_cutover()
        self.two_cabinets()
        self.due_window()
        # Cabinet 1 is checked under its first requirement version, then under a second one;
        # cabinet 4 once. Each check is a minute apart.
        self.check(self.version_a)
        self.clock.set(self.clock.now + timedelta(minutes=1))
        self.check(self.version_b)
        self.clock.set(self.clock.now + timedelta(minutes=1))
        self.publish_requirement(self.cabinet1_requirement, compartment_id=self.cabinet_row())
        self.clock.set(self.clock.now + timedelta(minutes=1))
        self.check(self.version_a)
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)
        item = str(self.suction.pk)
        self.keys = {
            (self.vehicle_id, item, self.cabinet1_requirement),
            (self.vehicle_id, item, self.cabinet4_requirement),
        }
        times = sorted(
            CheckRecord.objects.filter(
                organization_id=self.org_id, item_requirement_version_id__isnull=False
            ).values_list("recorded_at", flat=True)
        )
        self.assertEqual(len(times), 3)
        micro = timedelta(microseconds=1)
        edge = {t + d for t in times for d in (-micro, timedelta(0), micro)}
        self.instants = sorted(edge | {times[0] - timedelta(days=1), times[-1] + timedelta(days=1)})

    def test_the_first_requirement_has_two_versions(self):
        units = projections._Units(self.keys)
        self.assertEqual(units.requirements, {k[2] for k in self.keys})
        self.assertEqual(len(units.versions[self.cabinet1_requirement]), 2)

    def test_equals_the_per_key_lookup_at_every_boundary(self):
        for keys in (self.keys, *({k} for k in self.keys)):
            for lower in self.instants:
                with self.subTest(keys=sorted(keys), lower=lower):
                    self.assertEqual(
                        projections._newest_older_by_key(self.org_id, keys, lower),
                        reference_newest(self.org_id, keys, lower),
                    )

    def test_a_chunk_of_one_gives_the_same_answer(self):
        with mock.patch.object(projections, "LOOKUP_CHUNK", 1):
            for lower in self.instants:
                with self.subTest(lower=lower):
                    self.assertEqual(
                        projections._newest_older_by_key(self.org_id, self.keys, lower),
                        reference_newest(self.org_id, self.keys, lower),
                    )

    def test_the_keys_are_one_statement_not_one_query_each(self):
        lower = self.instants[-1]
        with CaptureQueriesContext(connection) as queries:
            projections._newest_older_by_key(
                self.org_id, self.keys, lower, projections._Units(self.keys)
            )
        reads = [q["sql"] for q in queries.captured_queries if "check_record" in q["sql"]]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].count("check_record"), 1)

    def test_another_organization_sees_nothing(self):
        from uuid import uuid4

        self.assertEqual(
            projections._newest_older_by_key(str(uuid4()), self.keys, self.instants[-1]), {}
        )
