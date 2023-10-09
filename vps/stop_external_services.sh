#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for name in bridge slave; do
  pkill -f "${base_dir}/${name}.py" >/dev/null 2>&1 || true
  pkill -f "python3 .*${name}.py .*external_ci.conf" >/dev/null 2>&1 || true
  echo "stopped ${name} if it was running"
done
