"""deploy/rocky9/cya-manage, run offline against stand-ins for systemd (issue #6 host profile).

The real script runs under bash with systemd-run, systemctl, journalctl and sleep replaced by
small scripts on PATH. They record each call and play back a scripted sequence of unit states,
so the tests cover the result handling, the interrupt path and the deferred cleanup without
systemd, root or a host.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / "deploy" / "rocky9" / "cya-manage"
UNIT = re.compile(r"^cya-manage-[0-9]{14}-[0-9]+$")

STUBS = {
    # Each call's arguments, NUL-separated, one file per call.
    "systemd-run": """#!/bin/bash
n=$(ls "$STUB_DIR"/run.* 2>/dev/null | wc -l)
printf '%s\\0' "$@" > "$STUB_DIR/run.$n"
exit "$(cat "$STUB_DIR/run_exit" 2>/dev/null || echo 0)"
""",
    # "show --property=P --value UNIT" plays back $STUB_DIR/states, one "STATE START" line
    # per ActiveState query (the last line repeats); stop and reset-failed are only recorded.
    "systemctl": """#!/bin/bash
echo "$*" >> "$STUB_DIR/systemctl"
[ "$1" = show ] || exit 0
mapfile -t lines < "$STUB_DIR/states"
n=$(cat "$STUB_DIR/n" 2>/dev/null || echo 0)
last=$(( ${#lines[@]} - 1 ))
case "${2#--property=}" in
  ActiveState)
    i=$(( n < last ? n : last )); echo $((n + 1)) > "$STUB_DIR/n"
    set -- ${lines[$i]}; echo "$1" ;;
  ExecMainStartTimestampMonotonic)
    i=$(( n - 1 < last ? n - 1 : last )); [ "$i" -lt 0 ] && i=0
    set -- ${lines[$i]}; echo "${2:-1}" ;;
  LoadState) cat "$STUB_DIR/load" 2>/dev/null || echo loaded ;;
  ExecMainStatus) cat "$STUB_DIR/status" 2>/dev/null || echo 0 ;;
  Result) cat "$STUB_DIR/result" 2>/dev/null || echo success ;;
esac
""",
    # The follower prints a line and then waits until it is stopped.
    "journalctl": """#!/bin/bash
echo "command output"
echo $$ > "$STUB_DIR/follower"
exec "$REAL_SLEEP" 30
""",
    # No delay, except that the runner's first sleep waits until the follower has started;
    # optionally interrupts the runner (its parent) on the Nth call.
    "sleep": """#!/bin/bash
if [ "${WAIT_FOLLOWER:-}" = 1 ]; then
  while [ ! -s "$STUB_DIR/follower" ]; do "$REAL_SLEEP" 0.01; done
fi
n=$(( $(cat "$STUB_DIR/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/sleeps"
if [ "$n" = "${INTERRUPT_AT:-}" ]; then kill -"${INTERRUPT_SIGNAL:-INT}" "$PPID"; fi
exit 0
""",
}


class CyaManageTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, text in STUBS.items():
            path = bin_dir / name
            path.write_text(text)
            path.chmod(0o700)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
            "REAL_SLEEP": shutil.which("sleep"),
        }

    def run_script(self, *args, states=("activating 0", "active 1"), **extra):
        (self.dir / "states").write_text("\n".join(states) + "\n")
        for name in ("status", "result", "load", "run_exit"):
            if name in extra:
                (self.dir / name).write_text(str(extra.pop(name)) + "\n")
        env = {**self.env, **{key: str(value) for key, value in extra.items()}}
        env["WAIT_FOLLOWER"] = "0" if args[:1] == ("--cleanup-after",) else "1"
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
        )

    def runs(self):
        """Each systemd-run call's arguments, in order."""
        calls = sorted(self.dir.glob("run.*"), key=lambda path: int(path.suffix[1:]))
        return [path.read_text().split("\0")[:-1] for path in calls]

    def systemctl_actions(self):
        path = self.dir / "systemctl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if not line.startswith("show ")]

    def unit(self, run):
        name = next(arg for arg in run if arg.startswith("--unit=")).removeprefix("--unit=")
        self.assertRegex(name, UNIT)
        return name

    def assert_follower_stopped(self):
        pid = int((self.dir / "follower").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_success_runs_one_detached_unit_and_cleans_it_up(self):
        result = self.run_script("owner", "create_organization", "--name", "Two Words")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("command output", result.stdout)
        [run] = self.runs()
        unit = self.unit(run)
        for expected in ("--no-block", "RemainAfterExit=yes", "--uid=cya"):
            self.assertIn(expected, run)
        self.assertIn("EnvironmentFile=/etc/check-your-ambulance/owner.env", run)
        # manage.py arguments pass through as separate words, never through a shell.
        self.assertEqual(run[-4:], ["manage.py", "create_organization", "--name", "Two Words"])
        self.assertEqual(self.systemctl_actions(), [f"stop {unit}", f"reset-failed {unit}"])
        self.assert_follower_stopped()

    def test_dba_runs_as_root_with_the_dba_file(self):
        result = self.run_script("dba", "audit_runtime_grants", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        [run] = self.runs()
        self.assertIn("--uid=root", run)
        self.assertIn("EnvironmentFile=/etc/check-your-ambulance/dba.env", run)

    def test_the_command_exit_status_is_returned(self):
        result = self.run_script("runtime", "check", status=3, result="exit-code")
        self.assertEqual(result.returncode, 3)
        self.assertIn("result=exit-code status=3", result.stderr)
        unit = self.unit(self.runs()[0])
        self.assertEqual(self.systemctl_actions(), [f"stop {unit}", f"reset-failed {unit}"])

    def test_a_failure_without_an_exit_status_still_fails(self):
        result = self.run_script("owner", "migrate", status=0, result="timeout")
        self.assertEqual(result.returncode, 1)
        self.assertIn("result=timeout", result.stderr)

    def test_it_waits_while_the_unit_has_not_started(self):
        states = ("inactive 0", "inactive 0", "activating 1", "activating 1", "active 1")
        result = self.run_script("owner", "check", states=states)
        self.assertEqual(result.returncode, 0, result.stderr)
        queries = (self.dir / "n").read_text().strip()
        self.assertEqual(queries, "5")  # it stopped polling only at the ended state

    def test_unknown_role_or_missing_role_is_refused_before_anything_runs(self):
        for args in (("root", "check"), ()):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: cya-manage", result.stderr)
                self.assertEqual(self.runs(), [])

    def test_systemd_run_failure_stops_before_following(self):
        result = self.run_script("owner", "check", run_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dir / "follower").exists())
        self.assertEqual(self.systemctl_actions(), [])

    def assert_interrupt_hands_cleanup_to_a_helper(self, signal):
        states = ("activating 1",)  # the command is still running when interrupted
        result = self.run_script(
            "owner", "migrate", states=states, INTERRUPT_AT=2, INTERRUPT_SIGNAL=signal
        )
        self.assertEqual(result.returncode, 130)
        self.assertIn("Interrupted: the command continues as", result.stderr)
        main, helper = self.runs()
        unit = self.unit(main)
        self.assertIn(f"--unit={unit}-cleanup", helper)
        self.assertIn("--no-block", helper)
        self.assertEqual(helper[-3:], [str(SCRIPT.resolve()), "--cleanup-after", unit])
        # The runner leaves the unit to the helper and stops its own follower.
        self.assertEqual(self.systemctl_actions(), [])
        self.assert_follower_stopped()
        return unit

    def test_ctrl_c_stops_the_follower_and_hands_cleanup_to_a_helper(self):
        self.assert_interrupt_hands_cleanup_to_a_helper("INT")

    def test_hang_up_is_handled_like_ctrl_c(self):
        self.assert_interrupt_hands_cleanup_to_a_helper("HUP")

    def test_the_helper_waits_for_the_command_then_stops_and_resets_it(self):
        unit = self.assert_interrupt_hands_cleanup_to_a_helper("INT")
        for name in ("n", "systemctl", "sleeps"):
            (self.dir / name).unlink(missing_ok=True)
        states = ("activating 1", "activating 1", "active 1")
        result = self.run_script("--cleanup-after", unit, states=states)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{unit} ended with result=success status=0", result.stdout)
        self.assertEqual((self.dir / "n").read_text().strip(), "3")
        self.assertEqual(self.systemctl_actions(), [f"stop {unit}", f"reset-failed {unit}"])

    def test_the_helper_treats_a_missing_unit_as_ended(self):
        result = self.run_script(
            "--cleanup-after",
            "cya-manage-20261103000000-42",
            states=("inactive 0",),
            load="not-found",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / "sleeps").exists())

    def test_the_helper_refuses_a_malformed_unit_name(self):
        for name in ("", "cya.service", "cya-manage-1-2", "cya-manage-20261103000000-1; rm"):
            with self.subTest(name=name):
                result = self.run_script("--cleanup-after", name)
                self.assertEqual(result.returncode, 2)
                self.assertIn("bad unit name", result.stderr)
                self.assertEqual(self.systemctl_actions(), [])
