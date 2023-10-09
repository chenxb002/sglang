#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="${1:-${base_dir}/external_ci.conf}"
log_dir="${base_dir}/logs"
mkdir -p "${log_dir}"

start_one() {
  local name="$1"
  shift
  if pgrep -f "${base_dir}/${name}.py ${conf}" >/dev/null 2>&1; then
    echo "${name} is already running"
    return
  fi
  nohup setsid python3 -u "${base_dir}/${name}.py" "${conf}" "$@" >"${log_dir}/${name}.log" 2>&1 &
  echo "started ${name}: pid=$!, log=${log_dir}/${name}.log"
}

start_one slave
start_one bridge
