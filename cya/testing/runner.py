"""Django test runner that keeps the whole test run offline, in every process."""

from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import iter_test_cases

from cya.testing.network import NetworkGuard, guard_active
from cya.testing.sharding import parse_shard, select


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
    """DiscoverRunner with the network guard installed for the entire run.

    `--shard K/N` runs only the test classes in shard K of N (cya.testing.sharding; issue #158).
    """

    parallel_test_suite = OfflineParallelTestSuite

    def __init__(self, *args, shard=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.shard = shard
        self._loaded = self._selected = 0

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--shard",
            type=parse_shard,
            metavar="K/N",
            help="Run only the test classes in shard K of N (split by a stable class hash).",
        )

    def load_tests_for_label(self, label, discover_kwargs):
        tests = super().load_tests_for_label(label, discover_kwargs)
        if self.shard is None or tests is None:
            return tests
        loaded = list(iter_test_cases(tests))
        selected = select(loaded, *self.shard)
        self._loaded += len(loaded)
        self._selected += len(selected)
        return self.test_suite(selected)

    def build_suite(self, test_labels=None, **kwargs):
        self._loaded = self._selected = 0
        suite = super().build_suite(test_labels, **kwargs)
        if self.shard is not None:
            index, count = self.shard
            self.log(
                f"Shard {index}/{count}: {self._selected} of {self._loaded} loaded test(s), "
                "before tag filtering."
            )
            # A shard of a non-empty suite that selects nothing means a broken split, not a
            # pass: stop rather than report "Ran 0 tests" as success.
            if self._loaded and not self._selected:
                raise RuntimeError(f"shard {index}/{count} selected none of {self._loaded} tests")
        return suite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._network_guard = NetworkGuard()
        self._network_guard.install()

    def teardown_test_environment(self, **kwargs):
        self._network_guard.uninstall()
        super().teardown_test_environment(**kwargs)
