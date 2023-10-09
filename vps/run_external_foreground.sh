#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="${1:-${base_dir}/external_ci.conf}"

python3 -u "${base_dir}/slave.py" "${conf}" &
slave_pid="$!"
python3 -u "${base_dir}/bridge.py" "${conf}" &
bridge_pid="$!"

shutdown() {
  kill "${slave_pid}" "${bridge_pid}" >/dev/null 2>&1 || true
  wait >/dev/null 2>&1 || true
}
trap shutdown INT TERM

# If either service exits, stop the other one so the container can restart.
wait -n "${slave_pid}" "${bridge_pid}"
shutdown
