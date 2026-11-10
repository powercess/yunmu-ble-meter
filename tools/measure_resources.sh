#!/usr/bin/env bash
# Measure a running collector's CPU/memory footprint.
# usage: measure_resources.sh PID [SECONDS]
set -euo pipefail
pid=${1:?usage: measure_resources.sh PID [SECONDS]}
secs=${2:-60}
hz=$(getconf CLK_TCK)
read -r u1 s1 <<<"$(awk '{print $14, $15}' "/proc/$pid/stat")"
sleep "$secs"
read -r u2 s2 <<<"$(awk '{print $14, $15}' "/proc/$pid/stat")"
awk -v u1="$u1" -v s1="$s1" -v u2="$u2" -v s2="$s2" -v hz="$hz" -v secs="$secs" \
  'BEGIN { printf "cpu_pct=%.2f\n", ((u2-u1)+(s2-s1))/hz/secs*100 }'
grep -E 'VmRSS|VmHWM|VmSize|Threads' "/proc/$pid/status"
echo "fds=$(find /proc/$pid/fd -maxdepth 1 | wc -l)"
