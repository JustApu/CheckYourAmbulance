"""Known long-running test classes start first within their ordering group (issue #57)."""

import unittest
from importlib import import_module
from unittest import mock

from django.test import SimpleTestCase, TestCase, TransactionTestCase

from cya.testing import runner


# Stand-in classes; their names are what slow_first sees. Their only method is not named
# test_*, so discovery runs none of them.
class FastCase(TestCase):
    def probe(self):
        pass


class SlowCase(TestCase):
    def probe(self):
        pass


class FastTransaction(TransactionTestCase):
    def probe(self):
        pass


class SlowerTransaction(TransactionTestCase):
    def probe(self):
        pass


class SlowTransaction(TransactionTestCase):
    def probe(self):
        pass


class PlainCase(unittest.TestCase):
    def probe(self):
        pass


def subsuite(cls):
    return unittest.TestSuite([cls("probe")])


def names(subsuites):
    return [type(next(iter(s))).__name__ for s in subsuites]


def label(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


class SlowFirstTests(SimpleTestCase):
    def test_every_listed_class_still_exists_and_is_a_test_class(self):
        self.assertEqual(len(set(runner.SLOW_FIRST)), len(runner.SLOW_FIRST))
        for name in runner.SLOW_FIRST:
            with self.subTest(name=name):
                module, _, attribute = name.rpartition(".")
                found = getattr(import_module(module), attribute)
                self.assertTrue(issubclass(found, SimpleTestCase))

    def test_listed_classes_lead_their_group_and_nothing_else_moves(self):
        order = [
            FastCase,
            SlowCase,
            FastTransaction,
            SlowTransaction,
            SlowerTransaction,
            PlainCase,
        ]
        listed = (label(SlowerTransaction), label(SlowCase), label(SlowTransaction), "gone.Case")
        with mock.patch.object(runner, "SLOW_FIRST", listed):
            result = runner.slow_first([subsuite(cls) for cls in order])
        self.assertEqual(
            names(result),
            [
                "SlowCase",  # TestCase group first, as Django orders it
                "FastCase",
                "SlowerTransaction",  # then the TransactionTestCase group, in list order
                "SlowTransaction",
                "FastTransaction",
                "PlainCase",  # then classes outside Django's test classes
            ],
        )

    def test_a_transaction_class_never_moves_ahead_of_a_testcase(self):
        # Within a worker a TestCase must not follow a TransactionTestCase, which flushes the
        # database, so even a listed TransactionTestCase stays behind every TestCase.
        listed = (label(SlowTransaction),)
        with mock.patch.object(runner, "SLOW_FIRST", listed):
            result = runner.slow_first([subsuite(FastCase), subsuite(SlowTransaction)])
        self.assertEqual(names(result), ["FastCase", "SlowTransaction"])

    def test_without_listed_classes_the_order_is_unchanged(self):
        order = [FastCase, SlowCase, FastTransaction, PlainCase]
        with mock.patch.object(runner, "SLOW_FIRST", ()):
            result = runner.slow_first([subsuite(cls) for cls in order])
        self.assertEqual(names(result), [cls.__name__ for cls in order])

    def test_the_parallel_suite_hands_out_the_reordered_subsuites(self):
        listed = (label(SlowCase),)
        with mock.patch.object(runner, "SLOW_FIRST", listed):
            suite = runner.OfflineParallelTestSuite(
                [subsuite(FastCase), subsuite(SlowCase)], processes=2
            )
        self.assertEqual(names(suite.subsuites), ["SlowCase", "FastCase"])
