#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkill -f "${base_dir}/master.py" >/dev/null 2>&1 || true
pkill -f "python3 .*master.py .*internal_master.conf" >/dev/null 2>&1 || true
echo "stopped master if it was running"
