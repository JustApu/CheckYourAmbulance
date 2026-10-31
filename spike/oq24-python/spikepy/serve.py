"""THROWAWAY spike helper: serve the ASGI app with Daphne on loopback. Usage: python -m spikepy.serve PORT"""
import sys

from . import dj

dj.configure("unused")
from daphne.cli import CommandLineInterface  # noqa: E402

CommandLineInterface().run(["-b", "127.0.0.1", "-p", sys.argv[1], "-v", "0", "spikepy.asgi:application"])
