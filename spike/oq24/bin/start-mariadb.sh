#!/usr/bin/env bash
# THROWAWAY spike helper (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): start a private, socket-only MariaDB
# instance in a fresh directory. Prints the directory; stop with stop-db.sh.
set -euo pipefail
base="${1:?usage: start-mariadb.sh BASE_DIR}"
# Unix socket paths are limited to ~107 bytes, so keep the socket in a short private dir.
sockdir="$(mktemp -d /tmp/spk.XXXXXX)"
chmod 0700 "$sockdir"
touch "$sockdir/.spike-sockdir"
echo "$sockdir" > "$base.sockdir"
mkdir -p "$base"/{data,binlog,tmp}
chmod 0700 "$base" "$base"/{data,binlog,tmp}
touch "$base/.spike-instance"
mariadb-install-db --no-defaults --user=root --datadir="$base/data" \
  --auth-root-authentication-method=normal --skip-test-db >"$base/install.log" 2>&1
mariadbd --no-defaults --user=root --datadir="$base/data" \
  --socket="$sockdir/s" --pid-file="$base/pid" --skip-networking \
  --tmpdir="$base/tmp" --log-error="$base/err.log" \
  --log-bin="$base/binlog/bin" --server-id=1 --binlog-format=ROW \
  >/dev/null 2>&1 &
for _ in $(seq 1 60); do
  if mariadb --no-defaults -uroot -S "$sockdir/s" -e 'SELECT 1' >/dev/null 2>&1; then
    echo "BASE=$base SOCK=$sockdir/s"; exit 0
  fi
  sleep 1
done
echo "mariadb did not start; see $base/err.log" >&2
exit 1
