"""CI test sharding (issue #158 item 4, option 3): every test class runs in exactly one shard."""

import argparse
import importlib.util
import io
import unittest
from collections import defaultdict
from contextlib import redirect_stderr
from pathlib import Path

from django.test import SimpleTestCase
from django.test.utils import iter_test_cases

from cya.testing import sharding
from cya.testing.runner import OfflineTestRunner

ROOT = Path(__file__).resolve().parents[2]


def configured_shard_count():
    """DJANGO_SHARDS from tools/ci_lanes.py, the count the CI workflow uses."""
    spec = importlib.util.spec_from_file_location(
        "_ci_lanes_for_sharding", ROOT / "tools/ci_lanes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DJANGO_SHARDS


def load(shard=None, labels=("cya",)):
    runner = OfflineTestRunner(verbosity=0, shard=shard)
    return list(iter_test_cases(runner.build_suite(list(labels))))


class ShardOptionTests(SimpleTestCase):
    def test_parses_k_of_n(self):
        self.assertEqual(sharding.parse_shard("1/1"), (1, 1))
        self.assertEqual(sharding.parse_shard("2/3"), (2, 3))
        self.assertEqual(sharding.parse_shard("64/64"), (64, 64))

    def test_refuses_malformed_or_out_of_range_values(self):
        for value in (
            "",
            "3",
            "/3",
            "1/",
            "0/3",
            "4/3",
            "1/0",
            "1/65",
            "a/b",
            "-1/3",
            "1/3/5",
            " 1/3",
            "１/3",
            "1.0/3",
        ):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                sharding.parse_shard(value)

    def test_the_runner_accepts_the_option(self):
        parser = argparse.ArgumentParser()
        OfflineTestRunner.add_arguments(parser)
        self.assertEqual(parser.parse_args(["--shard", "2/3"]).shard, (2, 3))
        self.assertIsNone(parser.parse_args([]).shard)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(["--shard", "4/3"])

    def test_the_split_is_a_stable_hash_not_pythons_salted_hash(self):
        # Fixed values: a change to the hash would silently move classes between shards.
        self.assertEqual(sharding.shard_of("cya.example.tests.Example1Tests", 3), 1)
        self.assertEqual(sharding.shard_of("cya.example.tests.Example2Tests", 3), 3)
        self.assertEqual(sharding.shard_of("anything", 1), 1)
        for count in (0, -1, 65, "3", None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                sharding.shard_of("x", count)

    def test_a_test_that_failed_to_load_is_kept_in_every_shard(self):
        failed = list(
            iter_test_cases(unittest.TestLoader().loadTestsFromName("cya.no_such_module_158"))
        )
        self.assertTrue(failed and all(sharding.is_load_failure(test) for test in failed))
        for index in (1, 2, 3):
            self.assertEqual(sharding.select(failed, index, 3), failed)

    def test_a_shard_that_selects_nothing_from_its_labels_stops_the_run(self):
        label = f"{__name__}.ShardOptionTests"
        own = sharding.shard_of(f"{__name__}.ShardOptionTests", 2)
        self.assertTrue(load((own, 2), labels=(label,)))
        with self.assertRaisesRegex(RuntimeError, "selected none"):
            load((3 - own, 2), labels=(label,))


class ShardCompletenessTests(SimpleTestCase):
    """Every class of the whole suite lands in exactly one shard, with all of its tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.everything = load()
        cls.ids = [test.id() for test in cls.everything]

    def assert_partition(self, shards):
        seen = defaultdict(list)
        classes = defaultdict(set)
        for index, tests in shards.items():
            for test in tests:
                seen[test.id()].append(index)
                classes[sharding.class_key(test)].add(index)
        self.assertEqual(sorted(seen), sorted(self.ids), "the shards do not cover the suite")
        repeated = {test_id: where for test_id, where in seen.items() if len(where) != 1}
        self.assertEqual(repeated, {}, "tests in more than one shard")
        split = {name: where for name, where in classes.items() if len(where) != 1}
        self.assertEqual(split, {}, "classes split across shards")

    def test_the_suite_is_large_and_loads_without_errors(self):
        self.assertGreater(len(self.everything), 1000)
        self.assertEqual(len(self.ids), len(set(self.ids)))
        self.assertFalse([t.id() for t in self.everything if sharding.is_load_failure(t)])

    def test_the_configured_ci_shards_partition_the_suite_through_the_runner(self):
        count = configured_shard_count()
        self.assertGreaterEqual(count, 1)
        shards = {index: load((index, count)) for index in range(1, count + 1)}
        for index, tests in shards.items():
            with self.subTest(shard=index):
                self.assertTrue(tests, "an empty shard wastes a CI job")
        self.assert_partition(shards)

    def test_other_shard_counts_also_partition_the_suite(self):
        for count in (1, 2, 4, 5, 8):
            with self.subTest(count=count):
                self.assert_partition(
                    {
                        index: sharding.select(self.everything, index, count)
                        for index in range(1, count + 1)
                    }
                )
