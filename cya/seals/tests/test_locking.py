"""Fail closed when a complete seal lock set is unavailable."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from cya.seals.locking import SealLockError, require_item_locks


class SealLockTests(SimpleTestCase):
    def test_every_physical_item_needs_the_vehicle_item_lock(self):
        scope = [SimpleNamespace(item_id="item-a"), SimpleNamespace(item_id="item-b")]
        require_item_locks(scope, {"item-a", "item-b"})
        with self.assertRaises(SealLockError):
            require_item_locks(scope, {"item-a"})
