#!/usr/bin/env bash
# THROWAWAY spike test: the deletion guard in bin/stop-db.sh must refuse path traversal, symlinks,
# unmarked directories and look-alike paths, and must remove a correctly marked instance directory.
# Uses only fresh directories created by mktemp; touches nothing else. Run from the repository root.
set -uo pipefail
stop=spike/oq24/bin/stop-db.sh
work="$(mktemp -d /tmp/spkguardXXXXXX)"
trap 'rm -rf -- "$work"' EXIT
fail=0
check() { local name="$1" want="$2"; shift 2; "$@" >/dev/null 2>&1; local rc=$?
  if { [[ "$want" == refuse && $rc -ne 0 ]] || [[ "$want" == ok && $rc -eq 0 ]]; }; then echo "ok - $name"; else echo "not ok - $name (rc=$rc)"; fail=1; fi; }
exists() { [[ -e "$1" ]]; }

mkdir -p "$work/spike-db" "$work/important"
echo precious > "$work/important/file"
touch "$work/important/.spike-instance"

# traversal out of a spike-db directory
check "refuses traversal ($work/spike-db/../important)" refuse "$stop" "$work/spike-db/../important"
check "important directory still present after traversal attempt" ok exists "$work/important/file"
# symlink inside spike-db pointing outside
ln -s "$work/important" "$work/spike-db/link"
check "refuses a symlink to another directory" refuse "$stop" "$work/spike-db/link"
check "symlink target untouched" ok exists "$work/important/file"
# directory under spike-db but without the instance marker
mkdir -p "$work/spike-db/unmarked"; echo x > "$work/spike-db/unmarked/file"
check "refuses an unmarked directory" refuse "$stop" "$work/spike-db/unmarked"
check "unmarked directory untouched" ok exists "$work/spike-db/unmarked/file"
# marked directory NOT directly under a spike-db directory (look-alike parent)
mkdir -p "$work/not-spike/inst"; touch "$work/not-spike/inst/.spike-instance"
check "refuses a marked directory with the wrong parent" refuse "$stop" "$work/not-spike/inst"
# nested one level too deep
mkdir -p "$work/spike-db/a/b"; touch "$work/spike-db/a/b/.spike-instance"
check "refuses a grandchild of spike-db" refuse "$stop" "$work/spike-db/a/b"
# socket directory recorded but pointing somewhere unmarked: nothing may be removed
mkdir -p "$work/spike-db/inst2"; touch "$work/spike-db/inst2/.spike-instance"
echo "$work/important" > "$work/spike-db/inst2.sockdir"
check "refuses a socket-dir record outside /tmp/spk.*" refuse "$stop" "$work/spike-db/inst2"
check "instance directory kept when the socket check fails" ok exists "$work/spike-db/inst2/.spike-instance"
check "important directory still present after socket attempt" ok exists "$work/important/file"
rm -f "$work/spike-db/inst2.sockdir"
# a correctly marked instance with a marked socket directory is removed
mkdir -p "$work/spike-db/inst"; touch "$work/spike-db/inst/.spike-instance"
sd="$(mktemp -d /tmp/spk.XXXXXX)"; touch "$sd/.spike-sockdir"; echo "$sd" > "$work/spike-db/inst.sockdir"
check "removes a correctly marked instance" ok "$stop" "$work/spike-db/inst"
check "instance directory removed" refuse exists "$work/spike-db/inst"
check "socket directory removed" refuse exists "$sd"
check "important directory still present at the end" ok exists "$work/important/file"
exit $fail
