# OQ-24 architecture spike ([issue #15](https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) — THROWAWAY prototypes

**This is not application code and not an approved stack.** It exists only to produce evidence for
the stack recommendation in [`docs/spikes/oq24-architecture-spike.md`](../../docs/spikes/oq24-architecture-spike.md).
Do not build the application on it, and do not treat its schema as the real schema (that is the M1
contracts issue). All data is synthetic; the database accounts and passwords are local test values.

## What is here

| Path | Purpose |
|---|---|
| `sql/schema.sql` | Tiny schema subset used to exercise database behavior (append-only tables, triggers, least-privilege account) |
| `lib/Spike/` | Helpers: connections, seed data, the check-recording algorithm, DST resolution, token validation |
| `t/01-privileges.t` … `t/07-push-csrf.t` | One test file per spike criterion |
| `bin/` | Scripts that start/stop **private, socket-only** database instances and a one-route SSE probe. `stop-db.sh` only deletes canonicalized, marked directories directly under a `spike-db` directory; `t/stop-guard.sh` tests that guard (traversal, symlinks, look-alike paths) |

## How to run

Requires a Linux host with Perl 5.38+, a MariaDB 10.11 or MySQL 8.0 server **binary** (the scripts start
a private instance; they never touch a system service or `data/`), and project-local Perl modules in
`local/` (gitignored): `Mojolicious DBI DBD::MariaDB DateTime Crypt::JWT`.

```sh
cpanm --notest -L local Mojolicious DBI DBD::MariaDB DateTime DateTime::TimeZone Crypt::JWT

# MariaDB (example)
spike/oq24/bin/start-mariadb.sh "$SCRATCH/spike-db/mariadb"        # prints BASE= and SOCK=
export SPIKE_FLAVOR=mariadb SPIKE_SOCK=<SOCK> SPIKE_CLIENT=mariadb \
       SPIKE_DUMP=mariadb-dump SPIKE_BINLOG=mariadb-binlog SPIKE_BASE=<BASE>
prove -I local/lib/perl5 -I spike/oq24/lib spike/oq24/t

# MySQL 8.0: binaries extracted (not installed) from the distribution packages
spike/oq24/bin/start-mysql.sh "$SCRATCH/spike-db/mysql" "$MYSQL_ROOT"
export SPIKE_FLAVOR=mysql SPIKE_SOCK=<SOCK> SPIKE_CLIENT=$MYSQL_ROOT/usr/bin/mysql \
       SPIKE_DUMP=$MYSQL_ROOT/usr/bin/mysqldump SPIKE_BINLOG=$MYSQL_ROOT/usr/bin/mysqlbinlog SPIKE_BASE=<BASE>
prove -I local/lib/perl5 -I spike/oq24/lib spike/oq24/t

spike/oq24/bin/stop-db.sh "$BASE"   # stops the instance and removes only its own directory
```

`$SCRATCH/spike-db` must be a path containing `/spike-db/` (the stop script refuses to delete anything else).
Tests create and drop their own uniquely named databases. No test uses the network except the
loopback SSE probe (127.0.0.1) and the loopback database socket.
