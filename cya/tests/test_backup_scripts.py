"""deploy/backup scripts, run offline against stand-ins for MariaDB, GnuPG and rsync (issue #7).

The real scripts run under bash with mariadb, mariadb-dump, mariadb-binlog, gpg and rsync replaced
on PATH by small scripts that record their arguments. "Encryption" is a copy, so the tests can
read what would have been uploaded. gzip, gunzip and flock are the real ones.
"""

import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SCRIPTS = Path(settings.BASE_DIR) / "deploy" / "backup"
POSITION = "-- CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000004', MASTER_LOG_POS=344;\n"

STUBS = {
    "mariadb": r"""#!/bin/bash
printf '%s\0' "$@" > "$STUB_DIR/mariadb.$(date +%s%N)"
query=""
while [ $# -gt 0 ]; do
  case "$1" in -N) ;; -e) query=$2; shift ;; *) target=$1 ;; esac
  shift
done
case "$query" in
  "") cat > "$STUB_DIR/loaded.$target.$(date +%s%N)" ;;
  "FLUSH BINARY LOGS") echo flush >> "$STUB_DIR/flushes" ;;
  "SHOW MASTER STATUS") printf '%s\t120\n' "$(cat "$STUB_DIR/current")" ;;
  "SELECT @@log_bin_basename") echo "$STUB_DIR/binlogs/mariadb-bin" ;;
  "SHOW BINARY LOGS") for f in $(ls "$STUB_DIR/binlogs" | sort); do printf '%s\t9\n' "$f"; done ;;
  SELECT\ 1\ FROM\ information_schema*) grep -q "'$(cat "$STUB_DIR/existing" 2>/dev/null)'" <<<"$query" && echo 1 ;;
  CREATE\ DATABASE*) echo "$query" >> "$STUB_DIR/created" ;;
esac
exit 0
""",
    "mariadb-dump": r"""#!/bin/bash
printf '%s\0' "$@" > "$STUB_DIR/dump-args"
[ -n "${DUMP_FAIL:-}" ] && { echo "synthetic dump failure" >&2; exit 2; }
printf '%s' "$DUMP_POSITION"
echo "CREATE TABLE synthetic (id int);"
""",
    "gpg": r"""#!/bin/bash
out=""; decrypt=""; file=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; --decrypt) decrypt=1 ;; --recipient) echo "$2" >> "$STUB_DIR/recipients"; shift ;;
    --homedir) shift ;; -*) ;; *) file=$1 ;; esac
  shift
done
if [ -n "$decrypt" ]; then cat "$file"; else cat > "$out"; fi
""",
    "rsync": r"""#!/bin/bash
args=("$@"); dest=${args[-1]}; src=${args[-2]}
echo "$src -> $dest" >> "$STUB_DIR/uploads"
[ -n "${RSYNC_FAIL:-}" ] && { echo "synthetic upload failure" >&2; exit 12; }
mkdir -p "$STUB_DIR/store"; cp "$src" "$STUB_DIR/store/"
""",
    "mariadb-binlog": r"""#!/bin/bash
echo "TZ=$TZ" > "$STUB_DIR/binlog-env"
printf '%s\0' "$@" > "$STUB_DIR/binlog-args"
echo "-- synthetic replay"
""",
}


class BackupScriptCase(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, text in STUBS.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o700)
        self.conf = self.dir / "conf"
        self.conf.mkdir()
        (self.conf / "backup.conf").write_text(
            "DATABASE=cya_synthetic\nSTORE=backup@store.example.test:\nRECIPIENT=SYNTHETICKEY\n"
        )
        self.state = self.dir / "state"
        (self.dir / "binlogs").mkdir()
        (self.dir / "current").write_text("mariadb-bin.000005\n")
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_DIR": str(self.dir),
            "CYA_BACKUP_CONF": str(self.conf),
            "CYA_BACKUP_STATE": str(self.state),
            "CYA_RESTORE_TMP": str(self.dir),
            "DUMP_POSITION": POSITION,
        }

    def run_script(self, name, *args, **extra):
        env = {**self.env, **{key: str(value) for key, value in extra.items()}}
        return subprocess.run(
            ["bash", str(SCRIPTS / name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def lines(self, name):
        path = self.dir / name
        return path.read_text().splitlines() if path.exists() else []

    def args(self, name):
        return (self.dir / name).read_text().split("\0")[:-1]

    def binlog(self, name, text="binary log"):
        (self.dir / "binlogs" / name).write_text(text)


class FullBackupTests(BackupScriptCase):
    def test_a_full_backup_flushes_dumps_encrypts_and_uploads(self):
        result = self.run_script("cya-backup-full")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "binlog-from").read_text(), "mariadb-bin.000005\n")
        dump = self.args("dump-args")
        for option in ("--single-transaction", "--master-data=2", "--routines", "--triggers"):
            self.assertIn(option, dump)
        self.assertEqual(dump[-1], "cya_synthetic")
        self.assertEqual(self.lines("recipients"), ["SYNTHETICKEY"])
        (uploaded,) = list((self.dir / "store").iterdir())
        self.assertRegex(uploaded.name, r"^full-cya_synthetic-\d{8}T\d{6}Z\.sql\.gz\.gpg$")
        self.assertTrue(gzip.decompress(uploaded.read_bytes()).decode().startswith(POSITION))
        self.assertTrue(self.lines("uploads")[0].endswith(" -> backup@store.example.test:"))
        self.assertEqual([p.name for p in self.state.iterdir() if p.is_dir()], [])  # spool gone

    def test_a_failed_dump_or_upload_fails_the_backup(self):
        for failure in ({"DUMP_FAIL": 1}, {"RSYNC_FAIL": 1}):
            with self.subTest(failure=failure):
                shutil.rmtree(self.dir / "store", ignore_errors=True)
                result = self.run_script("cya-backup-full", **failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.dir / "store").exists())

    def test_a_failed_backup_keeps_the_previous_shipping_start(self):
        # The last good backup started at 000003; 000004 is closed but not shipped yet.
        self.state.mkdir()
        (self.state / "binlog-from").write_text("mariadb-bin.000003\n")
        for name in ("mariadb-bin.000003", "mariadb-bin.000004", "mariadb-bin.000005"):
            self.binlog(name, f"log {name}")
        (self.state / "shipped").write_text("mariadb-bin.000003\n")
        for failure in ({"DUMP_FAIL": 1}, {"RSYNC_FAIL": 1}):
            with self.subTest(failure=failure):
                result = self.run_script("cya-backup-full", **failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.state / "binlog-from").read_text(), "mariadb-bin.000003\n")
                self.assertFalse((self.state / "binlog-from.new").exists())
        # Shipping still continues the previous backup's chain: 000004 goes out.
        (self.dir / "current").write_text("mariadb-bin.000006\n")
        shipped = self.run_script("cya-backup-binlog")
        self.assertEqual(shipped.returncode, 0, shipped.stderr)
        self.assertEqual(
            self.lines("state/shipped"),
            ["mariadb-bin.000003", "mariadb-bin.000004", "mariadb-bin.000005"],
        )
        # A successful backup then moves the start.
        self.assertEqual(self.run_script("cya-backup-full").returncode, 0)
        self.assertEqual((self.state / "binlog-from").read_text(), "mariadb-bin.000006\n")

    def test_a_missing_setting_stops_before_anything_runs(self):
        (self.conf / "backup.conf").write_text("DATABASE=cya_synthetic\n")
        result = self.run_script("cya-backup-full")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dir / "dump-args").exists())


class BinlogShippingTests(BackupScriptCase):
    def test_closed_logs_from_the_full_backup_are_shipped_once(self):
        for name in ("mariadb-bin.000003", "mariadb-bin.000004", "mariadb-bin.000005"):
            self.binlog(name, f"log {name}")
        self.state.mkdir()
        (self.state / "binlog-from").write_text("mariadb-bin.000004\n")
        result = self.run_script("cya-backup-binlog")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 000003 predates the full backup; 000005 is still being written.
        self.assertEqual(self.lines("state/shipped"), ["mariadb-bin.000004"])
        (uploaded,) = list((self.dir / "store").iterdir())
        self.assertEqual(uploaded.name, "binlog-mariadb-bin.000004.gz.gpg")
        self.assertEqual(gzip.decompress(uploaded.read_bytes()).decode(), "log mariadb-bin.000004")
        self.assertEqual(self.lines("flushes"), ["flush"])
        again = self.run_script("cya-backup-binlog")
        self.assertIn("shipped=0", again.stdout)
        self.assertEqual(len(self.lines("uploads")), 1)

    def test_a_failed_upload_is_retried_next_time(self):
        self.binlog("mariadb-bin.000004")
        self.state.mkdir()
        (self.state / "binlog-from").write_text("mariadb-bin.000004\n")
        failed = self.run_script("cya-backup-binlog", RSYNC_FAIL=1)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.lines("state/shipped"), [])
        retried = self.run_script("cya-backup-binlog")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.lines("state/shipped"), ["mariadb-bin.000004"])

    def test_no_full_backup_yet_ships_nothing(self):
        self.state.mkdir()
        result = self.run_script("cya-backup-binlog")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no full backup yet", result.stderr)
        self.assertEqual(self.lines("uploads"), [])


class RestoreTests(BackupScriptCase):
    STOP = "2026-06-01 12:00:00"

    def store(self, *binlogs, position=POSITION, name="offsite"):
        store = self.dir / name
        store.mkdir()
        dump = position + "CREATE TABLE synthetic (id int);\n"
        (store / "full-cya_synthetic-20260601T020000Z.sql.gz.gpg").write_bytes(
            gzip.compress(dump.encode())
        )
        for name in binlogs:
            (store / f"binlog-{name}.gz.gpg").write_bytes(gzip.compress(f"log {name}".encode()))
        return store

    def restore(self, store, *args):
        args = args or ("cya_synthetic", "cya_restored", self.STOP)
        return self.run_script("cya-restore", str(store), str(self.dir / "gnupg"), *args)

    def test_a_restore_loads_the_dump_and_replays_from_its_position_to_the_stop(self):
        store = self.store("mariadb-bin.000003", "mariadb-bin.000004", "mariadb-bin.000005")
        result = self.restore(store)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE DATABASE `cya_restored`", self.lines("created")[0])
        loads = sorted(p for p in self.dir.iterdir() if p.name.startswith("loaded.cya_restored"))
        self.assertEqual(len(loads), 2)  # the dump, then the replay
        self.assertIn("SET SESSION sql_log_bin=0;", loads[0].read_text())
        self.assertIn("CREATE TABLE synthetic", loads[0].read_text())
        replay = self.args("binlog-args")
        self.assertIn("--rewrite-db=cya_synthetic->cya_restored", replay)
        self.assertIn("--database=cya_synthetic", replay)  # filtered before the rewrite
        self.assertIn("--start-position=344", replay)
        self.assertIn(f"--stop-datetime={self.STOP}", replay)
        self.assertEqual(
            [Path(p).name for p in replay if not p.startswith("--")],
            ["mariadb-bin.000004", "mariadb-bin.000005"],  # from the dump's log, in order
        )
        self.assertEqual(self.lines("binlog-env"), ["TZ=UTC"])
        self.assertEqual(list(self.dir.glob("cya-restore.*")), [])  # work directory removed

    def test_refusals_change_nothing(self):
        store = self.store("mariadb-bin.000004")
        cases = (
            ((), "usage"),
            (("cya_synthetic", "Bad-Name", self.STOP), "bad database name"),
            (("cya_synthetic", "cya_restored", "2026-06-01T12:00"), "stop must be"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_script(
                    "cya-restore", *((str(store), "gnupg", *args) if args else ())
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
        (self.dir / "existing").write_text("cya_restored")
        exists = self.restore(store)
        self.assertEqual(exists.returncode, 1)
        self.assertIn("already exists", exists.stderr)
        self.assertEqual(self.lines("created"), [])

    def test_a_missing_backup_position_or_log_stops_the_restore(self):
        empty = self.dir / "empty-store"
        empty.mkdir()
        self.assertIn("no full backup", self.restore(empty).stderr)
        no_position = self.store("mariadb-bin.000004", position="", name="no-position")
        self.assertIn("no binary-log position", self.restore(no_position).stderr)
        self.assertEqual(self.lines("created"), [])
        missing_log = self.store("mariadb-bin.000005", name="gap")  # 000004 never shipped
        result = self.restore(missing_log)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mariadb-bin.000004 was not shipped", result.stderr)
        self.assertEqual(self.lines("created"), [])  # checked before anything is created

    def test_a_gap_in_the_log_chain_stops_the_restore_before_anything_is_created(self):
        gap = self.store("mariadb-bin.000004", "mariadb-bin.000006", name="gap-in-middle")
        result = self.restore(gap)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "mariadb-bin.000005 was not shipped (gap before mariadb-bin.000006)", result.stderr
        )
        self.assertEqual(self.lines("created"), [])
        self.assertFalse((self.dir / "binlog-args").exists())
        unbroken = self.store(
            "mariadb-bin.000004", "mariadb-bin.000005", "mariadb-bin.000006", name="unbroken"
        )
        result = self.restore(unbroken)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(mariadb-bin.000004 to mariadb-bin.000006)", result.stdout)
