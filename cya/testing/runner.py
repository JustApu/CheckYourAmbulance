"""Django test runner that keeps the whole test run offline, in every process."""

from django.test.runner import DiscoverRunner, ParallelTestSuite

from cya.testing.network import NetworkGuard, guard_active


def install_worker_guard():
    """Make sure the network guard is active in this process; raise if it cannot be.

    A parallel worker started by fork inherits the main process's guard. One started by
    forkserver (Python 3.14's default on Linux) or spawn (macOS) imports everything afresh and
    would otherwise run its tests unguarded (issue #53).
    """
    if not guard_active():
        NetworkGuard().install()
    if not guard_active():
        raise RuntimeError("the test network guard is not active in this worker")


def _init_guarded_worker(counter, *args, **kwargs):
    install_worker_guard()
    ParallelTestSuite.init_worker(counter, *args, **kwargs)


class OfflineParallelTestSuite(ParallelTestSuite):
    """Each worker installs the guard before Django sets it up, whatever the start method.

    Django's worker initializer runs in every worker; if it raises, Django stops the run, so a
    worker that cannot install the guard never runs a test.
    """

    init_worker = _init_guarded_worker


class OfflineTestRunner(DiscoverRunner):
    """DiscoverRunner with the network guard installed for the entire run."""

    parallel_test_suite = OfflineParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._network_guard = NetworkGuard()
        self._network_guard.install()

    def teardown_test_environment(self, **kwargs):
        self._network_guard.uninstall()
        super().teardown_test_environment(**kwargs)
