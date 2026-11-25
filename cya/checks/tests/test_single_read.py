"""Issue #278 Part F: a save's single linking read equals the separate reads it replaces.

Each class below reruns an existing linking or conflict suite with every save cross-checked:
links_and_conflict_obligation must return the same links (obligation, ordinal, timing, rule)
and the same conflict anchor as choose_links plus conflict_obligation for that check and instant.
"""

from unittest import mock

from django.test import SimpleTestCase

from cya.checks import commands, linking
from cya.checks.tests import test_conflicts, test_obligation_links, test_shared_completion
from cya.obligations.tests import test_requirement_obligations


def _shape(links):
    return [
        (str(link["obligation"].pk), link["ordinal"], link["timing"], link["shared_rule"])
        for link in links
    ]


class CrossChecked:
    def setUp(self):
        super().setUp()
        real = linking.links_and_conflict_obligation
        mismatches, calls = [], []

        def checked(check, *, recorded_at):
            links, first = real(check, recorded_at=recorded_at)
            expected_links = linking.choose_links(check, recorded_at=recorded_at)
            expected_first = linking.conflict_obligation(check, recorded_at=recorded_at)
            calls.append(1)
            if _shape(links) != _shape(expected_links) or first != expected_first:
                mismatches.append((_shape(links), first, _shape(expected_links), expected_first))
            return links, first

        patcher = mock.patch.object(commands, "links_and_conflict_obligation", checked)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual(mismatches, []))
        self.single_read_calls = calls


class SingleReadLinkTests(CrossChecked, test_obligation_links.CheckObligationLinkTests):
    pass


class SingleReadKitLinkTests(CrossChecked, test_obligation_links.KitObligationLinkTests):
    pass


class SingleReadConflictTests(CrossChecked, test_conflicts.ConflictTests):
    pass


class SingleReadKitConflictTests(CrossChecked, test_conflicts.KitConflictTests):
    pass


class SingleReadSameRequirementTests(CrossChecked, test_shared_completion.SameRequirementTests):
    pass


class SingleReadInterimGuardTests(CrossChecked, test_shared_completion.InterimGuardTests):
    pass


class SingleReadRequirementStreamTests(
    CrossChecked, test_requirement_obligations.RequirementStreamTests
):
    pass


class SingleReadTransitionTests(CrossChecked, test_requirement_obligations.TransitionTests):
    pass


class SingleReadBoundTests(SimpleTestCase):
    """At the resolver bound the single read falls back to the separate reads unchanged."""

    def test_falls_back_when_the_read_reaches_the_bound(self):
        with (
            mock.patch.object(linking, "_scope", return_value={}),
            mock.patch.object(linking, "_stream_windows", return_value=None),
            mock.patch.object(linking, "choose_links", side_effect=linking.LinkScopeTooLarge),
            mock.patch.object(linking, "conflict_obligation") as conflict,
            self.assertRaises(linking.LinkScopeTooLarge),
        ):
            linking.links_and_conflict_obligation(object(), recorded_at=None)
        conflict.assert_not_called()

    def test_falls_back_to_both_separate_reads(self):
        with (
            mock.patch.object(linking, "_scope", return_value={}),
            mock.patch.object(linking, "_stream_windows", return_value=None),
            mock.patch.object(linking, "choose_links", return_value=["links"]),
            mock.patch.object(linking, "conflict_obligation", return_value="anchor"),
        ):
            result = linking.links_and_conflict_obligation(object(), recorded_at=None)
        self.assertEqual(result, (["links"], "anchor"))
