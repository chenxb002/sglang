#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="${1:-${base_dir}/internal_master.conf}"
log_dir="${base_dir}/logs"
mkdir -p "${log_dir}"

if pgrep -f "${base_dir}/master.py ${conf}" >/dev/null 2>&1; then
  echo "master is already running"
  exit 0
fi

nohup setsid python3 -u "${base_dir}/master.py" "${conf}" >"${log_dir}/master.log" 2>&1 &
echo "started master: pid=$!, log=${log_dir}/master.log"
