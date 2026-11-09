"""Projection reads that never read a vehicle's whole history (issue #63).

The checklist and the fleet dashboard used to read every check and deficiency a vehicle ever had,
so they failed once that passed MAX_ROWS (about 66 days of a 150-item vehicle). They now read the
chains linked to the window's obligations and the chains attention needs; deficiencies are
read in full as before. These tests check that:

* the answers equal those of the previous whole-history reader, kept below as the reference,
  over seeded weeks of checks, corrections, invalidations, deficiencies and kits;
* a history past the row bound still loads, for one vehicle and for a dashboard page;
* the hard cases of the attention search hold: a newest chain invalidated, and an item
  unchecked for weeks.
"""

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext

from cya.attention.readiness import fleet_readiness
from cya.attention.tests.support import AttentionCase
from cya.checks.commands import invalidate_chain
from cya.checks.models import CheckEvent, CheckObligationLink, CheckRecord, DeterminationCheck
from cya.checks.tests.test_obligation_links import ObligationLinkCleanupMixin
from cya.deficiencies.models import Deficiency, DeficiencyAffectedItem, DeficiencyEvent
from cya.kits.tests.support import KitCase
from cya.obligations import projections
from cya.obligations.projections import (
    get_vehicle_obligations_as_of,
    load_projection_histories,
    load_projection_history,
)
from cya.obligations.state import NO_KIT
from cya.obligations.tests.synthetic_history import generate_history


def reference_history(organization_id, vehicle_id, obligations, since, instant):
    """The whole-history reader this issue replaced (the reference), with the same row bound."""
    bounded = projections._bounded
    ids = tuple(sorted(obligations))
    item_ids = {o.item_id for o in obligations.values()}
    limit = None  # MAX_ROWS, read when called
    rows = bounded(
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id__in=item_ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id"),
        limit,
    )
    roots = {r.root_check_id for r in rows}
    decisions = bounded(
        CheckEvent.objects.filter(
            organization_id=organization_id, root_check_id__in=roots, recorded_at__lte=instant
        ).order_by("recorded_at", "id"),
        limit,
    )
    links = bounded(
        CheckObligationLink.objects.filter(
            organization_id=organization_id,
            obligation_id__in=ids,
            check_record__vehicle_id=vehicle_id,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id"),
        limit,
    )
    supports = bounded(
        DeterminationCheck.objects.filter(
            organization_id=organization_id,
            determination__obligation_id__in=ids,
            recorded_at__lte=instant,
        ).order_by("recorded_at", "id"),
        limit,
    )
    headers = bounded(
        Deficiency.objects.filter(
            organization_id=organization_id, vehicle_id=vehicle_id, recorded_at__lte=instant
        ).order_by("recorded_at", "id"),
        limit,
    )
    deficiency_ids = [d.pk for d in headers]
    items = {}
    for row in DeficiencyAffectedItem.objects.filter(
        organization_id=organization_id, deficiency_id__in=deficiency_ids, recorded_at__lte=instant
    ):
        items.setdefault(str(row.deficiency_id), set()).add(str(row.item_id))
    events = bounded(
        DeficiencyEvent.objects.filter(
            organization_id=organization_id,
            deficiency_id__in=deficiency_ids,
            recorded_at__lte=instant,
        ).order_by("sequence", "id"),
        limit,
    )
    return projections._history_fields(rows, decisions, links, supports, headers, items, events)


def reference_page(organization_id, obligations, since, instant):
    return {
        vehicle_id: reference_history(organization_id, vehicle_id, by_id, since, instant)
        for vehicle_id, by_id in obligations.items()
    }


class ReferenceAssertions:
    def states(self, vehicle, instant, **window):
        return get_vehicle_obligations_as_of(
            str(vehicle.pk), instant, organization_id=str(self.org.pk), **window
        )

    def assert_same_as_reference(self, instant, vehicles=None, **window):
        """Projection states and the readiness picture equal the whole-history reader's."""
        for vehicle in vehicles or [self.vehicle]:
            bounded = self.states(vehicle, instant, **window)
            with mock.patch.object(projections, "_database_history", reference_history):
                reference = self.states(vehicle, instant, **window)
            self.assertEqual(len(bounded), len(reference), f"instant={instant}")
            for mine, theirs in zip(bounded, reference, strict=True):
                if mine != theirs:
                    fields = {
                        name: (getattr(mine, name), getattr(theirs, name))
                        for name in mine.__dataclass_fields__
                        if getattr(mine, name) != getattr(theirs, name)
                    }
                    self.fail(f"instant={instant} obligation={mine.obligation.id}: {fields}")
        picture = fleet_readiness(str(self.org.pk), as_of=instant, now=self.clock.now)
        with mock.patch.object(projections, "_page_database_history", reference_page):
            expected = fleet_readiness(str(self.org.pk), as_of=instant, now=self.clock.now)
        self.assertEqual(picture, expected, f"readiness instant={instant}")


class SeededReferenceTests(ReferenceAssertions, ObligationLinkCleanupMixin, KitCase):
    """Weeks of checks, corrections, invalidations, deficiencies and kits: same answers."""

    def assert_seed(self, seed):
        window = {"start": self.at(), "end": self.at(days=17)}
        compared = 0
        for index, instant in enumerate(generate_history(self, seed=seed, checks=True, kits=True)):
            if index % 3 == 0:  # every third instant keeps the run short; all kinds still occur
                self.assert_same_as_reference(instant, **window)
                compared += 1
        self.assert_same_as_reference(self.clock.now, **window)
        self.assert_same_as_reference(self.clock.now)  # the default window as well
        self.assertGreater(compared, 10)
        # With the default window (the last 7 days of a 14-day history) the bounded read really
        # is smaller: settled history before the window was left out.
        mine = load_projection_history(
            str(self.vehicle.pk), self.clock.now, organization_id=str(self.org.pk)
        )
        all_checks = CheckRecord.objects.filter(vehicle=self.vehicle).count()
        self.assertLess(len(mine.check_records), all_checks)

    def test_a_seeded_history_gives_the_same_answers(self):
        self.assert_seed(6301)

    def test_another_seed_gives_the_same_answers(self):
        self.assert_seed(6302)


DAYS = 60  # 2 checks a day: well past the window's own evidence (about 81 rows)


class LongHistoryTests(ReferenceAssertions, ObligationLinkCleanupMixin, AttentionCase):
    """Weeks of daily checks: past the row bound, and the attention search's hard cases."""

    def setUp(self):
        super().setUp()
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        self.masks_at = None
        for day in range(DAYS):
            self.clock.set(self.at(days=day, hours=5, minutes=30))
            self.assertEqual(self.generate().failures, [])
            for vehicle in self.vehicles[:2]:
                self.record(self.suction, vehicle=vehicle)
                self.record(self.siren, vehicle=vehicle)
            if day == 0:  # masks are checked once, weeks before the reads
                self.masks_at = str(self.record(self.masks, quantity=6).body["check_id"])

    def test_history_past_the_row_bound_loads_for_a_vehicle_and_a_page(self):
        org, a = str(self.org.pk), str(self.vehicle.pk)
        history = load_projection_history(a, self.clock.now, organization_id=org)
        # The bound is set just large enough for what the window needs, and the vehicle's whole
        # check history is past it: the reference refuses, the bounded reader does not.
        bound = max(len(value) for value in vars(history).values() if isinstance(value, tuple))
        sizes = {k: len(v) for k, v in vars(history).items() if isinstance(v, tuple)}
        self.assertGreater(CheckRecord.objects.filter(vehicle=self.vehicle).count(), bound, sizes)
        self.assertLess(sizes["check_records"], bound)
        obligations = {o.id: o for o in history.obligations}
        with mock.patch.object(projections, "MAX_ROWS", bound):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                reference_history(org, a, obligations, None, self.clock.now)
            self.assertEqual(
                load_projection_history(a, self.clock.now, organization_id=org), history
            )
            page = load_projection_histories(
                [str(v.pk) for v in self.vehicles[:2]], self.clock.now, organization_id=org
            )
            self.assertEqual(page[a], history)
        self.assert_same_as_reference(self.clock.now, vehicles=self.vehicles[:2])

    def test_an_invalidated_newest_chain_falls_back_to_the_one_before(self):
        newest = str(self.record(self.suction).body["check_id"])
        invalidate_chain(
            check_id=newest, explanation="Synthetic invalidation.", **self.context(self.admin)
        )
        failed = str(self.failed(self.siren).body["check_id"])
        invalidate_chain(
            check_id=failed, explanation="Synthetic invalidation.", **self.context(self.admin)
        )
        self.assert_same_as_reference(self.clock.now)
        suction = self.row(self.suction)
        self.assertNotEqual(suction.latest_check_id, newest)
        self.assertEqual(
            suction.latest_check_id,
            str(
                CheckRecord.objects.filter(vehicle=self.vehicle, item=self.suction)
                .exclude(pk=newest)
                .latest("recorded_at")
                .pk
            ),
        )

    def test_an_item_unchecked_for_weeks_still_shows_its_last_check(self):
        self.assertEqual(self.row(self.masks).latest_check_id, self.masks_at)
        self.assert_same_as_reference(self.clock.now)


class PageBoundTests(ObligationLinkCleanupMixin, AttentionCase):
    def test_the_row_bound_is_per_vehicle_on_a_page(self):
        self.bind(vehicle=self.vehicles[1], reconcile=False)
        self.assertEqual(self.generate().failures, [])
        org = str(self.org.pk)
        ids = [str(v.pk) for v in self.vehicles[:2]]
        each = [
            len(load_projection_history(v, self.clock.now, organization_id=org).selections)
            for v in ids
        ]
        bound = max(each)
        self.assertGreater(sum(each), bound)  # the page together is over one vehicle's bound
        with mock.patch.object(projections, "MAX_ROWS", bound):
            page = load_projection_histories(ids, self.clock.now, organization_id=org)
            self.assertEqual([len(page[v].selections) for v in ids], each)
        with mock.patch.object(projections, "MAX_ROWS", bound - 1):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_projection_histories(ids, self.clock.now, organization_id=org)

    def test_deficiency_scope_rows_are_bounded_per_vehicle_in_a_page(self):
        # Vehicle A's deficiencies cover one more item row than the per-vehicle bound; vehicle B
        # has none. The page must refuse A, as A alone does, though the page-wide total fits.
        a, b = str(self.vehicles[0].pk), str(self.vehicles[1].pk)
        deficiency_id = str(self.failed(self.siren).body["deficiency_ids"][0])
        # The bound must admit A's other deficiency evidence; only the item rows exceed it.
        bound = max(
            2,
            Deficiency.objects.filter(vehicle_id=a).count(),
            DeficiencyEvent.objects.filter(deficiency__vehicle_id=a).count(),
        )
        rows = [
            mock.Mock(deficiency_id=deficiency_id, item_id=str(uuid4())) for _ in range(bound + 1)
        ]

        class Scope(list):
            def order_by(self, *fields):
                return self

        org, now = str(self.org.pk), self.clock.now
        with (
            mock.patch.object(projections, "MAX_ROWS", bound),
            mock.patch.object(
                projections.DeficiencyAffectedItem.objects,
                "filter",
                side_effect=lambda *args, **kwargs: Scope(rows),  # the current rows each call
            ),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                projections._history(org, {a: {}, b: {}}, now, now, bound * 2)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                projections._history(org, {a: {}}, now, now, bound)
            del rows[bound]  # exactly the bound fits, in the page and alone
            page = projections._history(org, {a: {}, b: {}}, now, now, bound * 2)
            self.assertEqual(page[b]["deficiency_headers"], ())
            self.assertEqual(page[a], projections._history(org, {a: {}}, now, now, bound)[a])


class SiblingKitTests(ReferenceAssertions, ObligationLinkCleanupMixin, AttentionCase):
    """A kit instance with no checks beside a sibling instance with a long history."""

    def test_an_unchecked_kit_does_not_walk_its_siblings_history(self):
        for day in range(30):
            self.clock.set(self.at(days=day, hours=5, minutes=30))
            self.assertEqual(self.generate().failures, [])
            self.record(self.kit_item, kit_id=self.reference)
        org, vehicle = str(self.org.pk), str(self.vehicle.pk)
        history = load_projection_history(vehicle, self.clock.now, organization_id=org)
        keys = {(o.item_id, o.kit_instance_id) for o in history.obligations}
        # Both kit instances have obligations; only the reference instance was ever checked.
        self.assertIn((str(self.kit_item.pk), str(self.kit_b)), keys)
        self.assertIn((str(self.kit_item.pk), str(self.reference)), keys)
        with CaptureQueriesContext(connection) as captured:
            load_projection_history(vehicle, self.clock.now, organization_id=org)
        walks = [q for q in captured.captured_queries if "FROM `check_record`" in q["sql"]]
        # The walk back stops at once for the unchecked instance; 30 days of the sibling's
        # history would take about 15 two-day steps of two queries each.
        self.assertLessEqual(len(walks), 6, [q["sql"][:120] for q in walks])
        page = load_projection_histories([vehicle], self.clock.now, organization_id=org)
        self.assertEqual(page[vehicle], history)
        self.assert_same_as_reference(self.clock.now)

    def test_a_long_gap_does_not_read_a_resolved_siblings_settled_history(self):
        # Kit A was checked once, a month ago; sibling kit B every day since. A's step back must
        # read only A's rows, not B's month of settled history inside the gap.
        self.clock.set(self.at(hours=5, minutes=30))
        self.assertEqual(self.generate().failures, [])
        a_check = str(self.record(self.kit_item, kit_id=self.reference).body["check_id"])
        for day in range(1, 31):
            self.clock.set(self.at(days=day, hours=5, minutes=30))
            self.assertEqual(self.generate().failures, [])
            self.record(self.kit_item, kit_id=self.kit_b)
        org, vehicle, now = str(self.org.pk), str(self.vehicle.pk), self.clock.now
        a_key = (vehicle, str(self.kit_item.pk), str(self.reference))
        # A bound of 5 rows admits A's one row, never B's 30.
        roots = projections._attention_roots(org, {a_key}, now, now, 5)
        self.assertEqual(roots, {a_check})
        history = load_projection_history(vehicle, now, organization_id=org)
        page = load_projection_histories([vehicle], now, organization_id=org)
        self.assertEqual(page[vehicle], history)
        self.assertIn(a_check, {r.snapshot.id for r in history.check_records})
        self.assert_same_as_reference(now)


def reference_newest_older(organization_id, keys, lower):
    """The shipped ORM union (one arm per key) that _newest_older_by_key replaced."""
    candidates = [
        CheckRecord.objects.filter(
            organization_id=organization_id,
            vehicle_id=vehicle_id,
            item_id=item_id,
            kit_id=None if kit_key == NO_KIT else kit_key,
            recorded_at__lte=lower,
        )
        .order_by("-recorded_at", "-id")
        .values_list("vehicle_id", "item_id", "kit_id", "recorded_at")[:1]
        for vehicle_id, item_id, kit_key in sorted(keys)
    ]
    return {
        projections._key(vehicle, item, kit): recorded
        for vehicle, item, kit, recorded in candidates[0].union(*candidates[1:], all=True)
    }


class SingleReferenceLookupEquivalenceTests(ObligationLinkCleanupMixin, AttentionCase):
    """The per-key 'newest check at or before T' lookup names check_record once per statement (issue #70:
    150 references opened 150 table instances per checklist read). It must return exactly what the ORM
    union it replaced returned; that union is kept above as the reference."""

    def setUp(self):
        super().setUp()
        for day in range(8):
            self.clock.set(self.at(days=day, hours=5, minutes=30))
            self.assertEqual(self.generate().failures, [])
            self.record(self.suction)
            self.record(self.siren)
            if day % 2 == 0:
                self.record(self.kit_item, kit_id=self.reference)
            if day in (1, 5):
                self.record(self.kit_item, kit_id=self.kit_b)
        self.org_id, self.vehicle_id = str(self.org.pk), str(self.vehicle.pk)
        item = str(self.kit_item.pk)
        self.no_kit = [
            (self.vehicle_id, str(self.suction.pk), NO_KIT),
            (self.vehicle_id, str(self.siren.pk), NO_KIT),
        ]
        self.kits = [
            (self.vehicle_id, item, str(self.reference)),
            (self.vehicle_id, item, str(self.kit_b)),
        ]
        self.absent = [
            (self.vehicle_id, item, NO_KIT),  # the kit item was only ever checked with a kit
            (self.vehicle_id, str(uuid4()), NO_KIT),  # an item with no checks at all
        ]
        times = sorted(
            set(
                CheckRecord.objects.filter(organization_id=self.org_id).values_list(
                    "recorded_at", flat=True
                )
            )
        )
        micro = timedelta(microseconds=1)
        edge = {t + d for t in times for d in (-micro, timedelta(0), micro)}
        self.instants = sorted(edge | {times[0] - timedelta(days=1), times[-1] + timedelta(days=1)})

    def key_sets(self):
        return [
            set(self.no_kit),
            set(self.kits),
            {self.kits[0]},
            {self.kits[1]},
            set(self.no_kit + self.kits),
            set(self.no_kit + self.kits + self.absent),
            set(self.absent),
        ]

    def test_equals_the_orm_union_for_no_kit_and_kit_keys_at_every_boundary(self):
        # The upper bound is inclusive: a check recorded exactly at T counts, one a microsecond later
        # does not; one a microsecond earlier does. The instants include all three for every check.
        for keys in self.key_sets():
            for lower in self.instants:
                with self.subTest(keys=len(keys), lower=lower):
                    self.assertEqual(
                        projections._newest_older_by_key(self.org_id, keys, lower),
                        reference_newest_older(self.org_id, keys, lower),
                    )

    def test_a_check_at_exactly_the_bound_counts_and_one_just_after_does_not(self):
        key = self.no_kit[0]
        stamps = sorted(
            CheckRecord.objects.filter(
                organization_id=self.org_id, vehicle_id=self.vehicle_id, item_id=key[1]
            ).values_list("recorded_at", flat=True)
        )
        at = stamps[3]
        self.assertEqual(projections._newest_older_by_key(self.org_id, {key}, at)[key], at)
        before = projections._newest_older_by_key(
            self.org_id, {key}, at - timedelta(microseconds=1)
        )
        self.assertEqual(before[key], stamps[2])

    def test_sibling_kit_instances_and_the_no_kit_key_stay_isolated(self):
        last = self.instants[-1]
        only_b = projections._newest_older_by_key(self.org_id, {self.kits[1]}, last)
        self.assertEqual(set(only_b), {projections._key(*self.kits[1])})
        # B was checked on days 1 and 5 only; A on days 0, 2, 4, 6: B's answer is never A's time.
        a = projections._newest_older_by_key(self.org_id, {self.kits[0]}, last)
        self.assertNotEqual(
            only_b[projections._key(*self.kits[1])], a[projections._key(*self.kits[0])]
        )
        # The kit item without its kit matches nothing (kit_id IS NULL is not 'any kit').
        self.assertEqual(projections._newest_older_by_key(self.org_id, {self.absent[0]}, last), {})
        # A key whose item has no checks is absent, not an error and not another key's time.
        self.assertEqual(projections._newest_older_by_key(self.org_id, {self.absent[1]}, last), {})

    def test_another_organization_sees_nothing(self):
        keys = set(self.no_kit + self.kits)
        last = self.instants[-1]
        self.assertTrue(projections._newest_older_by_key(self.org_id, keys, last))
        self.assertEqual(projections._newest_older_by_key(str(uuid4()), keys, last), {})
        self.assertEqual(reference_newest_older(str(uuid4()), keys, last), {})

    def test_chunking_gives_the_same_answer_and_each_statement_names_the_table_once(self):
        keys = set(self.no_kit + self.kits + self.absent)
        last = self.instants[-1]
        want = reference_newest_older(self.org_id, keys, last)
        with mock.patch.object(projections, "LOOKUP_CHUNK", 1):
            with CaptureQueriesContext(connection) as chunked:
                self.assertEqual(projections._newest_older_by_key(self.org_id, keys, last), want)
        with CaptureQueriesContext(connection) as whole:
            self.assertEqual(projections._newest_older_by_key(self.org_id, keys, last), want)
        # One statement per kind of key when it fits, one per key at chunk size 1.
        self.assertEqual(len(whole.captured_queries), 2)
        self.assertEqual(len(chunked.captured_queries), len(keys))
        table = CheckRecord._meta.db_table
        for query in [*whole.captured_queries, *chunked.captured_queries]:
            self.assertEqual(query["sql"].count(f" {table} "), 1, query["sql"][:200])

    def test_a_hundred_and_fifty_keys_still_name_the_table_once(self):
        keys = {(self.vehicle_id, str(uuid4()), NO_KIT) for _ in range(150)}
        with CaptureQueriesContext(connection) as captured:
            projections._newest_older_by_key(self.org_id, keys, self.instants[-1])
        self.assertEqual(len(captured.captured_queries), 1)
        sql = captured.captured_queries[0]["sql"]
        self.assertEqual(sql.count(f" {CheckRecord._meta.db_table} "), 1)
        self.assertEqual(sql.count("UNION ALL"), 149)  # 150 constant rows, not 150 table references

    def test_aware_datetimes_come_back(self):
        found = projections._newest_older_by_key(self.org_id, set(self.no_kit), self.instants[-1])
        self.assertTrue(found)
        for stamp in found.values():
            self.assertIsNotNone(stamp.tzinfo)

    def test_the_statement_orders_newest_first_and_takes_one_row_per_key(self):
        # check_record rejects a second row with an identical instant (the insert trigger ties each
        # row to its receipt), so a tie cannot be seeded. The answer is only the time, so a tie
        # could not change it either; the ordering that picks the newest is fixed by the SQL.
        with CaptureQueriesContext(connection) as captured:
            projections._newest_older_by_key(self.org_id, set(self.no_kit), self.instants[-1])
        sql = captured.captured_queries[0]["sql"]
        self.assertIn("ORDER BY c.recorded_at DESC LIMIT 1", sql)
        self.assertIn("c.recorded_at <= ", sql)

    def test_more_than_one_chunk_of_keys_across_vehicles_matches_the_union(self):
        # About 480 keys over three vehicles (two unknown to the organization) plus the real ones, at the
        # real LOOKUP_CHUNK: no-kit keys span 3 chunks, kit keys one, and the answer is unchanged.
        other_vehicles = [str(uuid4()), str(uuid4())]
        extra = {
            (vehicle, str(uuid4()), NO_KIT) for vehicle in [self.vehicle_id, *other_vehicles] * 160
        }
        extra |= {(vehicle, str(self.siren.pk), NO_KIT) for vehicle in other_vehicles}
        keys = set(self.no_kit + self.kits + self.absent) | extra
        self.assertGreater(len(extra), 2 * projections.LOOKUP_CHUNK)
        last = self.instants[-1]
        with CaptureQueriesContext(connection) as captured:
            found = projections._newest_older_by_key(self.org_id, keys, last)
        self.assertEqual(found, reference_newest_older(self.org_id, keys, last))
        self.assertEqual(set(found), {projections._key(*k) for k in self.no_kit + self.kits})
        no_kit_keys = sum(1 for k in keys if k[2] == NO_KIT)
        chunks = -(-no_kit_keys // projections.LOOKUP_CHUNK) + 1  # plus one statement of kit keys
        self.assertEqual(len(captured.captured_queries), chunks)

    def commit_between_chunks(self, keys, run, late=((7, 6, 0, 1),)):
        """Run `run()` with chunk size 1 and record qualifying checks just before the second lookup
        statement, as transactions stamped earlier but committed later would appear under READ
        COMMITTED (each statement sees what is committed when it starts).

        `late` lists (day, hour, minute, index) checks recorded then, index into the sorted keys.
        """
        ordered = sorted(keys)
        marker, seen = "ORDER BY c.recorded_at DESC LIMIT 1", []

        def commit_late(execute, sql, params, many, context):
            if marker in sql:
                seen.append(sql)
                if len(seen) == 2:
                    for day, hour, minute, index in late:
                        self.clock.set(self.at(days=day, hours=hour, minutes=minute))
                        self.record(self.item_for(ordered[index]))
            return execute(sql, params, many, context)

        with mock.patch.object(projections, "LOOKUP_CHUNK", 1):
            with connection.execute_wrapper(commit_late):
                result = run()
        self.assertEqual(len(seen), 2)
        return ordered[0], ordered[-1], result

    def item_for(self, key):
        return next(i for i in (self.suction, self.siren) if str(i.pk) == key[1])

    def test_a_check_committed_between_chunks_changes_only_its_own_key(self):
        # Each key's answer comes from one statement (as in the former union) and a check belongs
        # to exactly one key, so statements at different snapshots cannot make one key's answer
        # depend on another's: the key read before the commit gets the earlier answer, the key
        # read after it gets the newer one, and each equals a single-statement answer at a snapshot.
        keys, bound = set(self.no_kit), self.at(days=7, hours=12)
        before = projections._newest_older_by_key(self.org_id, keys, bound)
        first, second, skewed = self.commit_between_chunks(
            keys, lambda: projections._newest_older_by_key(self.org_id, keys, bound)
        )
        after = projections._newest_older_by_key(self.org_id, keys, bound)
        self.assertEqual(skewed[projections._key(*first)], before[projections._key(*first)])
        self.assertEqual(skewed[projections._key(*second)], after[projections._key(*second)])
        self.assertGreater(after[projections._key(*second)], before[projections._key(*second)])

    def test_the_roots_do_not_depend_on_which_chunk_saw_a_late_commit(self):
        # The final roots depend only on the evidence, never on how the lookup or windows fell
        # (the _attention_roots contract), so a commit between chunks gives the roots computed
        # from all of the evidence.
        keys, bound = set(self.no_kit), self.at(days=7, hours=12)
        key_set = {projections._key(*k) for k in keys}
        unseen = projections._attention_roots(self.org_id, key_set, bound, bound, 500)
        _first, _second, skewed = self.commit_between_chunks(
            keys, lambda: projections._attention_roots(self.org_id, key_set, bound, bound, 500)
        )
        settled = projections._attention_roots(self.org_id, key_set, bound, bound, 500)
        self.assertNotEqual(unseen, settled)  # the late check is observable in the roots
        self.assertEqual(skewed, settled)

    def test_two_late_commits_on_keys_without_history_give_each_key_a_valid_answer(self):
        # Keys A and B have no check at or before the bound. A qualifying check for A, then one
        # for B, commit between the chunk statements, so the first chunk saw neither and the
        # second sees both: B is returned and A is not. No single snapshot has B without A, and
        # the former union could not return it, but each key's answer is a state that key really
        # had during the call (A: before its check; B: after its), the keys have no invariant
        # between them, and the next read has both. That per-key guarantee is the contract.
        keys, bound = set(self.no_kit), self.at(hours=5)
        a, b = sorted(keys)
        self.assertEqual(projections._newest_older_by_key(self.org_id, keys, bound), {})
        _first, _last, skewed = self.commit_between_chunks(
            keys,
            lambda: projections._newest_older_by_key(self.org_id, keys, bound),
            late=((0, 4, 0, 0), (0, 4, 30, 1)),
        )
        self.assertEqual(set(skewed), {projections._key(*b)})
        settled = projections._newest_older_by_key(self.org_id, keys, bound)
        self.assertEqual(set(settled), {projections._key(*a), projections._key(*b)})
        self.assertEqual(skewed[projections._key(*b)], settled[projections._key(*b)])
