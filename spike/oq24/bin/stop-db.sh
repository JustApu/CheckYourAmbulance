#!/usr/bin/env bash
# THROWAWAY spike helper (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): stop a private instance started by start-*.sh and remove ONLY the
# directories those scripts created. Every deletion is guarded: the path is canonicalized, must not be
# a symlink, must be a direct child of a directory named "spike-db" (or /tmp/spk.* for the socket
# directory), and must carry the marker file the start script wrote. Nothing is stopped or removed
# until all checks pass.
set -euo pipefail

refuse() { echo "stop-db.sh: refusing: $1" >&2; exit 1; }

base_arg="${1:?usage: stop-db.sh BASE_DIR}"
[[ -d "$base_arg" && ! -L "$base_arg" ]] || refuse "not a plain directory: $base_arg"
base="$(realpath -e -- "$base_arg")"
[[ "$(basename -- "$(dirname -- "$base")")" == "spike-db" ]] || refuse "not a direct child of a spike-db directory: $base"
[[ -f "$base/.spike-instance" && ! -L "$base/.spike-instance" ]] || refuse "missing instance marker in $base"

sockdir=""
if [[ -f "$base.sockdir" && ! -L "$base.sockdir" ]]; then
  candidate="$(cat -- "$base.sockdir")"
  if [[ -d "$candidate" && ! -L "$candidate" ]]; then
    resolved="$(realpath -e -- "$candidate")"
    case "$resolved" in
      /tmp/spk.*) [[ -f "$resolved/.spike-sockdir" ]] && sockdir="$resolved" ;;
    esac
  fi
  [[ -n "$sockdir" ]] || refuse "socket directory path is not a marked /tmp/spk.* directory"
fi

# All checks passed: stop the server, then delete exactly the validated paths.
if [[ -f "$base/pid" && ! -L "$base/pid" ]]; then
  pid="$(cat -- "$base/pid")"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  fi
fi
[[ -n "$sockdir" ]] && rm -rf -- "$sockdir"
rm -f -- "$base.sockdir"
rm -rf -- "$base"
