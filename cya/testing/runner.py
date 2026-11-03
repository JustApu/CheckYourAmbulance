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


# Test classes that take minutes, longest first (issue #57; --durations 40 of the full local suite
# on main eb3a0ca, 2026-11-03). Django hands parallel workers one class at a time, in suite order, so a long
# class near the end of its group finishes the run on its own. Starting these first within their
# group lets them overlap the rest. Maintained by hand: add a class here when --durations shows it
# taking minutes. A name no longer in the suite is ignored; a test checks every name still exists.
SLOW_FIRST = (
    "cya.obligations.tests.test_kits.MigrationTests",
    "cya.deficiencies.tests.test_correction.CorrectionScopeMigrationTests",
    "cya.deficiencies.tests.test_migration_reversal.EmptyReversalTests",
    "cya.tests.test_audit_integrity_db.AuditSchemaLifecycleTests",
    "cya.deficiencies.tests.test_scope_expansion.MigrationTests",
    "cya.deficiencies.tests.test_verification_invalidation.MigrationTests",
    "cya.deficiencies.tests.test_verification.VerificationMigrationTests",
    "cya.deficiencies.tests.test_suspension.MigrationTests",
    "cya.obligations.tests.test_batch_projections.SeededBatchTests",
    "cya.checks.tests.test_obligation_links.CheckObligationLinkTests",
    "cya.obligations.tests.test_projections.CheckHistoryTests",
    "cya.obligations.tests.test_kits.KitHistoryTests",
)


def _class_name(subsuite):
    first = next(iter(subsuite), None)
    return f"{type(first).__module__}.{type(first).__qualname__}"


def _ordering_group(subsuite):
    """Django's ordering group: TestCase classes, then other Django test classes, then the rest.

    Within a worker no TestCase may follow a TransactionTestCase, which flushes the database
    (DiscoverRunner.reorder_by), so classes never move between these groups.
    """
    first = next(iter(subsuite), None)
    for index, kind in enumerate(DiscoverRunner.reorder_by):
        if isinstance(first, kind):
            return index
    return len(DiscoverRunner.reorder_by)


def slow_first(subsuites):
    """The subsuites with each SLOW_FIRST class moved to the front of its ordering group.

    Stable: every other class keeps its place relative to the rest, and groups keep their order.
    """
    rank = {name: index for index, name in enumerate(SLOW_FIRST)}
    order = sorted(
        range(len(subsuites)),
        key=lambda i: (
            _ordering_group(subsuites[i]),
            rank.get(_class_name(subsuites[i]), len(rank)),
            i,
        ),
    )
    return [subsuites[i] for i in order]


class OfflineParallelTestSuite(ParallelTestSuite):
    """Each worker installs the guard before Django sets it up, whatever the start method.

    Django's worker initializer runs in every worker; if it raises, Django stops the run, so a
    worker that cannot install the guard never runs a test. The known long-running classes are
    handed out first within their ordering group (issue #57).
    """

    init_worker = _init_guarded_worker

    def __init__(self, subsuites, *args, **kwargs):
        super().__init__(slow_first(subsuites), *args, **kwargs)


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
