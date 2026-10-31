#!/usr/bin/env bash
# THROWAWAY spike helper (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): start a private, socket-only MySQL 8.0 from binaries extracted
# (not installed) from the distribution packages. Usage: start-mysql.sh BASE_DIR MYSQL_ROOT_DIR
set -euo pipefail
base="${1:?usage: start-mysql.sh BASE_DIR MYSQL_ROOT_DIR}"
root="${2:?usage: start-mysql.sh BASE_DIR MYSQL_ROOT_DIR}"
mkdir -p "$base"/{data,binlog,tmp}
chmod 0700 "$base" "$base"/{data,binlog,tmp}
touch "$base/.spike-instance"
sockdir="$(mktemp -d /tmp/spk.XXXXXX)"; chmod 0700 "$sockdir"
touch "$sockdir/.spike-sockdir"
echo "$sockdir" > "$base.sockdir"
mysqld="$root/usr/sbin/mysqld"
"$mysqld" --no-defaults --user=root --initialize-insecure --datadir="$base/data" \
  --basedir="$root/usr" --lc-messages-dir="$root/usr/share/mysql" >"$base/install.log" 2>&1
"$mysqld" --no-defaults --user=root --datadir="$base/data" --basedir="$root/usr" \
  --lc-messages-dir="$root/usr/share/mysql" --socket="$sockdir/s" --pid-file="$base/pid" \
  --skip-networking --mysqlx=OFF --secure-file-priv=NULL --tmpdir="$base/tmp" --log-error="$base/err.log" \
  --log-bin="$base/binlog/bin" --server-id=1 --binlog-format=ROW \
  >/dev/null 2>&1 &
for _ in $(seq 1 90); do
  if "$root/usr/bin/mysql" --no-defaults -uroot -S "$sockdir/s" -e 'SELECT 1' >/dev/null 2>&1; then
    echo "BASE=$base SOCK=$sockdir/s"; exit 0
  fi
  sleep 1
done
echo "mysqld did not start; see $base/err.log" >&2; exit 1
