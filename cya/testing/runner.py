"""Django test runner that keeps the whole test run offline."""

from django.test.runner import DiscoverRunner

from cya.testing.network import NetworkGuard


class OfflineTestRunner(DiscoverRunner):
    """DiscoverRunner with the network guard installed for the entire run."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._network_guard = NetworkGuard()
        self._network_guard.install()

    def teardown_test_environment(self, **kwargs):
        self._network_guard.uninstall()
        super().teardown_test_environment(**kwargs)
