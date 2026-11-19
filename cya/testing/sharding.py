"""Split the Django test suite into shards by test class (issue #158).

CI runs the full suite as several parallel jobs. Each job passes `--shard K/N` to the test runner,
which keeps only the test classes whose stable hash falls in shard K of N. The hash is SHA-256 of
the class's module and qualified name, so every process and every job agrees on the split without
sharing any state, and each class, with all of its tests, lands in exactly one shard. Python's
built-in hash() is salted per process and must not be used here.

A test that failed to load (unittest's placeholder for a module that cannot be imported) is kept
in every shard, so a broken module is reported whichever shards run.
"""

import argparse
import hashlib
import unittest

# More shards than this is a typing mistake, not a CI layout.
MAX_SHARDS = 64


def class_key(test):
    """The dotted module and qualified class name that decides a test's shard."""
    cls = type(test)
    return f"{cls.__module__}.{cls.__qualname__}"


def shard_of(key, count):
    """The 1-based shard, out of count, that the class named key belongs to."""
    if not isinstance(count, int) or not 1 <= count <= MAX_SHARDS:
        raise ValueError(f"shard count must be 1 to {MAX_SHARDS}, not {count!r}")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def is_load_failure(test):
    return isinstance(test, unittest.loader._FailedTest)


def in_shard(test, index, count):
    return is_load_failure(test) or shard_of(class_key(test), count) == index


def select(tests, index, count):
    """The tests, in their original order, that shard index of count runs."""
    return [test for test in tests if in_shard(test, index, count)]


def parse_shard(value):
    """Parse "K/N" (1 <= K <= N <= MAX_SHARDS) for the runner's --shard option."""
    index, separator, count = str(value).partition("/")
    if (
        not separator
        or not index.isascii()
        or not index.isdigit()
        or not count.isascii()
        or not count.isdigit()
    ):
        raise argparse.ArgumentTypeError(f"expected K/N, such as 1/3, not {value!r}")
    index, count = int(index), int(count)
    if not 1 <= count <= MAX_SHARDS or not 1 <= index <= count:
        raise argparse.ArgumentTypeError(f"expected 1 <= K <= N <= {MAX_SHARDS}, not {value!r}")
    return index, count
