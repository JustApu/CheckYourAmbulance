"""THROWAWAY spike helpers: connections and schema loading. Synthetic data only.

Environment: SPIKE_SOCK (unix socket of a private MariaDB), SPIKE_CLIENT (mariadb CLI),
SPIKE_DUMP, SPIKE_BINLOG, SPIKE_BASE (only the restore test).
"""
import os
import random
import subprocess
import tempfile
from pathlib import Path

import MySQLdb

SCHEMA = Path(__file__).resolve().parents[2] / "oq24" / "sql" / "schema.sql"
STRICT = "STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"


def sock():
    return os.environ["SPIKE_SOCK"]


def connect(user="app", password="spike-local-only", db=None, autocommit=True):
    kwargs = dict(unix_socket=sock(), user=user, passwd=password, autocommit=autocommit)
    if db:
        kwargs["db"] = db
    conn = MySQLdb.connect(**kwargs)
    cur = conn.cursor()
    cur.execute("SET SESSION time_zone = '+00:00'")
    cur.execute("SET SESSION sql_mode = %s", (STRICT,))
    cur.close()
    return conn


def admin(db=None):
    return connect("root", "", db)


def new_db_name():
    return f"spkpy_{os.getpid()}_{random.randrange(10**6)}"


def load_schema(db):
    adm = admin()
    adm.cursor().execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4")
    adm.close()
    sql = SCHEMA.read_text().replace("{DB}", f"`{db}`")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", prefix="spkpyschema", delete=True) as tmp:
        tmp.write(sql)
        tmp.flush()
        with open(tmp.name, "rb") as stdin:
            subprocess.run([os.environ.get("SPIKE_CLIENT", "mariadb"), "--no-defaults", "-uroot", "-S", sock(), db],
                           stdin=stdin, check=True)


def seed(adm, n_users=3):
    cur = adm.cursor()
    cur.execute("INSERT INTO vehicles (name) VALUES ('Synthetic Ambulance 1')")
    cur.execute("INSERT INTO items (name) VALUES ('Synthetic suction unit'), ('Synthetic oxygen masks')")
    users = []
    for i in range(1, n_users + 1):
        cur.execute("INSERT INTO users (idp_subject, display_name) VALUES (%s,%s)", (f"synthetic-sub-{i}", f"EMT {i}"))
        users.append(cur.lastrowid)
    lock_rows(adm)
    return users


def lock_rows(adm):
    adm.cursor().execute("INSERT IGNORE INTO item_lock (vehicle_id, item_id) "
                         "SELECT v.vehicle_id, i.item_id FROM vehicles v CROSS JOIN items i")


def drop_db(db):
    adm = admin()
    adm.cursor().execute(f"DROP DATABASE IF EXISTS `{db}`")
    adm.close()
