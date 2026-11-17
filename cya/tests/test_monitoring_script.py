"""deploy/monitoring/cya-alerts, offline (SPEC 4.2.0 DEP-13, AC-38; issue #8).

No network: SMTP is a fake class, the probe's fetch is injected, and systemctl and mariadb-admin
are replaced by callables. openssl is the real one, reading a certificate generated here.
"""

import datetime
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.test import SimpleTestCase

SCRIPT = Path(settings.BASE_DIR) / "deploy" / "monitoring" / "cya-alerts"
SECRET = "synthetic-relay-password-9"  # synthetic


def load_script():
    loader = importlib.machinery.SourceFileLoader("cya_alerts", str(SCRIPT))
    spec = importlib.util.spec_from_loader("cya_alerts", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


alerts = load_script()
HOUR = 3600


class FakeSMTP:
    instances = []
    refuse_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port)]
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.refuse_login:
            raise alerts.smtplib.SMTPAuthenticationError(535, b"no")
        self.calls.append(("login", user))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["Subject"], message.get_content()))
        return {}


class AlertsCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.relay_file = os.path.join(self.dir.name, "mail-relay.json")
        self.relay = {
            "host": "smtp-relay.example.test",
            "port": 587,
            "tls_mode": "STARTTLS",
            "auth_mode": "PASSWORD",
            "username": "relay-user",
            "password": SECRET,
            "sender": "alerts@example.test",
            "operator_addresses": ["operator@example.test"],
            "deputy_addresses": ["deputy@example.test"],
        }
        with open(self.relay_file, "w") as handle:
            json.dump(self.relay, handle)
        self.config = {
            **alerts.DEFAULTS,
            "relay_file": self.relay_file,
            "state_file": os.path.join(self.dir.name, "state", "state.json"),
        }
        self.sent = []

    def sender(self, relay, subject, body):
        self.sent.append((subject, body))

    def run_with(self, current, at, keep=None):
        return alerts.run(self.config, current, at, sender=self.sender, keep=keep)


class StateMachineTests(AlertsCase):
    def test_start_once_repeat_after_six_hours_then_clear_once(self):
        # AC-38(4): once at start, no sooner than 6 hours again, once on clear.
        current = {"database": "The database is refusing connections"}
        self.assertEqual(self.run_with(current, 1000.0), 0)
        self.run_with(current, 1000.0 + HOUR)
        self.run_with(current, 1000.0 + 6 * HOUR - 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ALERT: The database is refusing connections", self.sent[0][0])
        self.run_with(current, 1000.0 + 6 * HOUR)
        self.assertIn("STILL ACTIVE", self.sent[1][0])
        self.run_with({}, 1000.0 + 7 * HOUR)
        self.run_with({}, 1000.0 + 8 * HOUR)
        self.assertEqual(
            [s[0].split(":")[0] for s in self.sent][-1], "[Check Your Ambulance] CLEARED"
        )
        self.assertEqual(len(self.sent), 3)

    def test_a_failed_send_is_retried_next_run(self):
        def failing(relay, subject, body):
            raise alerts.SendError("CONNECT_FAILED")

        current = {"disk:/": "Low disk space on /: 5.0% free"}
        alerts.run(self.config, current, 1000.0, sender=failing)
        self.assertEqual(alerts.read_state(self.config["state_file"]), {})
        self.run_with(current, 1300.0)
        self.assertEqual(len(self.sent), 1)

    def test_the_state_file_is_mode_0600(self):
        self.run_with({"database": "x"}, 1000.0)
        mode = stat.S_IMODE(os.stat(self.config["state_file"]).st_mode)
        self.assertEqual(mode, 0o600)

    def test_probe_and_server_keys_do_not_clear_each_other(self):
        self.run_with({"database": "db"}, 1000.0, keep=lambda k: k.startswith("unreachable:"))
        url = "unreachable:https://cya.example.test/"
        self.run_with({url: "down"}, 1100.0, keep=lambda k: k != url)
        self.run_with({}, 1200.0, keep=lambda k: k != "unreachable:https://other.example.test/")
        # Only the run that owns no key here cleared nothing it does not own.
        state = alerts.read_state(self.config["state_file"])
        self.assertEqual(set(state), {"database", url})

    def test_an_unreadable_relay_copy_sends_nothing(self):
        os.unlink(self.relay_file)
        self.assertEqual(self.run_with({"database": "db"}, 1000.0), 2)
        self.assertEqual(self.sent, [])

    def test_alert_text_holds_no_secret_or_relay_setting(self):
        # AC-38(5).
        self.run_with({"database": "The database is refusing connections"}, 1000.0)
        subject, body = self.sent[0]
        for value in (SECRET, "relay-user", "smtp-relay.example.test", "operator@example.test"):
            self.assertNotIn(value, subject + body)


class ConditionTests(AlertsCase):
    def test_5xx_responses_in_the_window_are_counted(self):
        at = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.UTC).timestamp()
        line = '203.0.113.9 - - [16/Nov/2026:11:{m:02d}:00 +0000] "GET /x HTTP/1.1" {s} 12 "-" "ua"'
        lines = [line.format(m=m, s=502) for m in range(55, 60)]
        lines += [line.format(m=40, s=500), line.format(m=58, s=200), "garbage"]
        self.assertEqual(alerts.errors_in_window(lines, at, 600), 5)

    def test_iso_8601_access_log_times_are_counted(self):
        # Found in the issue #8 rehearsal: the Ubuntu host's nginx logs $time_iso8601.
        at = datetime.datetime(2026, 11, 17, 3, 0, tzinfo=datetime.UTC).timestamp()
        line = '198.51.100.7 [2026-11-17T02:{m:02d}:00+00:00] "GET / HTTP/2.0" {s} 0 0.1 "host"'
        lines = [line.format(m=m, s=502) for m in range(52, 58)] + [line.format(m=40, s=502)]
        lines.append('198.51.100.7 [2026-11-17T02:58:00Z] "GET / HTTP/2.0" 503 0 0.1 "host"')
        self.assertEqual(alerts.errors_in_window(lines, at, 600), 7)

    def test_error_condition_reads_only_the_log_tail(self):
        log = os.path.join(self.dir.name, "access.log")
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%d/%b/%Y:%H:%M:%S +0000")
        with open(log, "w") as handle:
            handle.write("x" * 5000 + "\n")
            for _ in range(25):
                handle.write(f'198.51.100.7 - - [{stamp}] "GET / HTTP/1.1" 503 0 "-" "ua"\n')
        config = {**self.config, "access_log": log, "access_log_tail_bytes": 4096}
        found = alerts.error_conditions(config, now.timestamp())
        self.assertEqual(list(found), ["5xx"])

    def test_failed_and_stale_jobs(self):
        at = 100_000.0

        def show(unit, names):
            return {
                "cya-obligations.service": {"LoadState": "loaded", "Result": "exit-code"},
                "cya-backup-full.service": {
                    "LoadState": "loaded",
                    "Result": "success",
                    "ExecMainExitTimestampUSec": str(int((at - 30 * HOUR) * 1_000_000)),
                },
                "cya-backup-binlog.service": {
                    "LoadState": "loaded",
                    "Result": "success",
                    "ExecMainExitTimestampUSec": str(int((at - 60) * 1_000_000)),
                },
            }.get(unit, {"LoadState": "not-found"})

        found = alerts.job_conditions(self.config, at, show=show)
        self.assertEqual(
            set(found), {"job:cya-obligations.service", "stale:cya-backup-full.service"}
        )

    def test_database_refusing_connections(self):
        ok = mock.Mock(return_value=mock.Mock(returncode=0))
        refused = mock.Mock(return_value=mock.Mock(returncode=1))
        self.assertEqual(alerts.database_conditions(self.config, 0, run=ok), {})
        self.assertEqual(
            list(alerts.database_conditions(self.config, 0, run=refused)), ["database"]
        )
        missing = mock.Mock(side_effect=FileNotFoundError)
        self.assertEqual(
            list(alerts.database_conditions(self.config, 0, run=missing)), ["database"]
        )

    def test_relay_copy_missing_or_old(self):
        at = os.stat(self.relay_file).st_mtime
        self.assertEqual(alerts.relay_copy_conditions(self.config, at + 60), {})
        self.assertEqual(
            list(alerts.relay_copy_conditions(self.config, at + 3 * HOUR)), ["relay-copy"]
        )
        os.unlink(self.relay_file)
        self.assertEqual(list(alerts.relay_copy_conditions(self.config, at)), ["relay-copy"])

    def test_certificate_expiring_within_14_days(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cya.example.test")])
        start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        paths = {}
        for label, days in (("soon", 5), ("later", 60)):
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(start)
                .not_valid_after(start + datetime.timedelta(days=days + 1))
                .sign(key, hashes.SHA256())
            )
            paths[label] = os.path.join(self.dir.name, f"{label}.pem")
            with open(paths[label], "wb") as handle:
                handle.write(cert.public_bytes(serialization.Encoding.PEM))
        config = {**self.config, "certificates": [paths["soon"], paths["later"], "/missing.pem"]}
        found = alerts.certificate_conditions(
            config, datetime.datetime.now(datetime.UTC).timestamp()
        )
        self.assertEqual(set(found), {f"certificate:{paths['soon']}", "certificate:/missing.pem"})
        self.assertIn("expires in", found[f"certificate:{paths['soon']}"])

    def test_low_disk_space(self):
        usage = mock.Mock(total=100, used=95, free=5)
        with mock.patch.object(alerts.shutil, "disk_usage", return_value=usage):
            self.assertEqual(
                list(alerts.disk_conditions(self.config, 0)), ["disk:/", "disk:/var/lib/mysql"]
            )

    def test_probe_alerts_after_two_failures_in_a_row(self):
        url = "https://cya.example.test/"
        down = lambda u: 502  # noqa: E731
        self.assertEqual(alerts.probe(self.config, url, 0, fetch=down), {})
        self.assertEqual(
            list(alerts.probe(self.config, url, 0, fetch=down)), [f"unreachable:{url}"]
        )
        self.assertEqual(alerts.probe(self.config, url, 0, fetch=lambda u: 200), {})


class SendTests(AlertsCase):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances, FakeSMTP.refuse_login = [], False

    def test_starttls_login_and_both_recipients(self):
        with mock.patch.object(alerts.smtplib, "SMTP", FakeSMTP):
            alerts.send(self.relay, "subject", "body")
        calls = FakeSMTP.instances[0].calls
        self.assertEqual([c[0] for c in calls], ["connect", "starttls", "login", "send"])
        self.assertEqual(calls[-1][1], "operator@example.test, deputy@example.test")

    def test_ip_authentication_does_not_log_in(self):
        relay = {**self.relay, "auth_mode": "IP", "username": "", "password": ""}
        with mock.patch.object(alerts.smtplib, "SMTP", FakeSMTP):
            alerts.send(relay, "subject", "body")
        self.assertNotIn("login", [c[0] for c in FakeSMTP.instances[0].calls])

    def test_failures_map_to_safe_codes(self):
        FakeSMTP.refuse_login = True
        with mock.patch.object(alerts.smtplib, "SMTP", FakeSMTP):
            with self.assertRaises(alerts.SendError) as caught:
                alerts.send(self.relay, "subject", "body")
        self.assertEqual(str(caught.exception), "AUTH_FAILED")
        refused = mock.Mock(side_effect=ConnectionRefusedError("detail with secrets"))
        with mock.patch.object(alerts.smtplib, "SMTP", refused):
            with self.assertRaises(alerts.SendError) as caught:
                alerts.send(self.relay, "subject", "body")
        self.assertEqual(str(caught.exception), "CONNECT_FAILED")

    def test_a_plaintext_relay_copy_is_refused(self):
        with open(self.relay_file, "w") as handle:
            json.dump({**self.relay, "tls_mode": "NONE"}, handle)
        with self.assertRaises(alerts.SendError):
            alerts.load_relay(self.relay_file)

    def test_the_script_parses_as_python_3_9(self):
        # Found in the issue #8 rehearsal: the probe host's python3 is 3.9 (Rocky Linux 9).
        import ast

        source = SCRIPT.read_text()
        ast.parse(source, feature_version=(3, 9))
        self.assertNotRegex(source, r"from datetime import[^\n]*\bUTC\b")

    def test_the_script_is_executable_and_stdlib_only(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        imports = [
            line.split()[1].split(".")[0]
            for line in SCRIPT.read_text().splitlines()
            if line.startswith(("import ", "from "))
        ]
        self.assertTrue(imports)
        for module in imports:
            self.assertIn(module, sys.stdlib_module_names, module)
