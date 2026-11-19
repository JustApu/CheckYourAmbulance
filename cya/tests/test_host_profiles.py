"""The Ubuntu host profile reuses the Rocky Linux 9 nginx configuration (issue #6).

deploy/ubuntu2604/nginx.conf is the deploy/rocky9 file with only the worker user and the
dynamic-modules path changed for Ubuntu's package, so a change to one must reach the other.

The recurring application jobs in deploy/rocky9 (both profiles install them) run as the
unprivileged identity with the runtime environment, never two at once, from a timer, and
are watched by the operations alerts (issues #6, #8, #222).
"""

import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

DEPLOY = Path(settings.BASE_DIR) / "deploy"
# The only directives that differ: Rocky's value, then Ubuntu's.
UBUNTU_CHANGES = {
    "user nginx;": "user www-data;",
    "include /usr/share/nginx/modules/*.conf;": "include /etc/nginx/modules-enabled/*.conf;",
}


def directives(path):
    """The configuration lines without comments, as Ubuntu would receive them."""
    lines = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(UBUNTU_CHANGES.get(line, line))
    return lines


class UbuntuNginxProfileTests(SimpleTestCase):
    def test_ubuntu_nginx_conf_is_the_rocky_file_with_only_the_ubuntu_lines_changed(self):
        rocky = directives(DEPLOY / "rocky9" / "nginx.conf")
        ubuntu = directives(DEPLOY / "ubuntu2604" / "nginx.conf")
        self.assertEqual(ubuntu, rocky)

    def test_each_ubuntu_change_applies_to_exactly_one_rocky_line(self):
        rocky = (DEPLOY / "rocky9" / "nginx.conf").read_text().splitlines()
        ubuntu = (DEPLOY / "ubuntu2604" / "nginx.conf").read_text().splitlines()
        for old, new in UBUNTU_CHANGES.items():
            with self.subTest(old):
                self.assertEqual(rocky.count(old), 1)
                self.assertEqual(ubuntu.count(new), 1)
                self.assertNotIn(old, ubuntu)


def unit_settings(path):
    """Each `Key=value` line of a systemd unit, as {key: [values]} (a key may repeat)."""
    found = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";", "[")):
            key, value = line.split("=", 1)
            found.setdefault(key.strip(), []).append(value.strip())
    return found


class RecurringJobUnitTests(SimpleTestCase):
    JOBS = sorted(
        path
        for path in (DEPLOY / "rocky9").glob("cya-*.service")
        if path.name != "cya.service"  # the application itself, not a job
    )

    def test_the_notification_jobs_are_present(self):
        names = {path.stem for path in self.JOBS}
        for name in ("cya-notify-delivery", "cya-notify-overdue", "cya-notify-check-due"):
            with self.subTest(name):
                self.assertIn(name, names)

    def test_each_job_runs_once_at_a_time_as_the_runtime_identity(self):
        for path in self.JOBS:
            with self.subTest(path.name):
                unit = unit_settings(path)
                self.assertEqual(unit["Type"], ["oneshot"])
                self.assertEqual(unit["User"], ["cya"])
                self.assertEqual(unit["Group"], ["cya"])
                self.assertEqual(unit["EnvironmentFile"], ["/etc/check-your-ambulance/runtime.env"])
                self.assertEqual(unit["WorkingDirectory"], ["/srv/cya/current"])
                self.assertEqual(len(unit["TimeoutStartSec"]), 1)
                for setting in ("NoNewPrivileges", "ProtectHome", "PrivateTmp", "PrivateDevices"):
                    self.assertEqual(unit[setting], ["yes"], setting)
                self.assertEqual(unit["ProtectSystem"], ["strict"])
                [command] = unit["ExecStart"]
                self.assertTrue(
                    command.startswith("/srv/cya/current/local/venv/bin/python manage.py ")
                )
                self.assertNotIn("owner", command)

    def test_each_job_has_a_timer_that_is_enabled_at_boot(self):
        for path in self.JOBS:
            with self.subTest(path.name):
                timer = unit_settings(path.with_suffix(".timer"))
                self.assertEqual(len(timer["OnCalendar"]), 1)
                self.assertEqual(timer["WantedBy"], ["timers.target"])

    def test_the_operations_alerts_watch_each_job(self):
        config = json.loads((DEPLOY / "monitoring" / "monitor.json.example").read_text())
        for path in self.JOBS:
            with self.subTest(path.name):
                self.assertIn(path.name, config["jobs"])

    def test_the_check_due_job_is_skipped_on_a_release_without_the_command(self):
        unit = unit_settings(DEPLOY / "rocky9" / "cya-notify-check-due.service")
        [condition] = unit["ConditionPathExists"]
        self.assertEqual(
            condition, "/srv/cya/current/cya/operations/management/commands/notify_check_due.py"
        )
        self.assertTrue(unit["ExecStart"][0].endswith(" manage.py notify_check_due --limit 50"))
