"""Seal event stream replay stays independent of current clock and configuration."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from cya.seals.commands import _event_state


class SealEventStateTests(SimpleTestCase):
    def test_break_and_audit_are_independent_until_fulfilled(self):
        seal = SimpleNamespace(event_type="RESEAL", marking="crew")
        audit = SimpleNamespace(event_type="AUDIT_REQUEST")
        broken = SimpleNamespace(event_type="BREAK")
        fulfilled = SimpleNamespace(event_type="AUDIT_FULFILLED")
        replacement = SimpleNamespace(event_type="RESEAL", marking="crew")
        self.assertEqual(_event_state([seal, audit]), (seal, True))
        self.assertEqual(_event_state([seal, audit, broken]), (None, True))
        self.assertEqual(
            _event_state([seal, audit, broken, replacement, fulfilled]), (replacement, False)
        )
