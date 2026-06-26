#!/usr/bin/env bash
# craw code AUTHORING benchmark: base Claude Code vs craw code.
# Makes live `claude -p` calls (costs money). NOT part of the pytest suite.
#
#   CRAW_BENCH_K=3 bash bench/authoring/author_bench.sh
#   python3 bench/authoring/evaluate.py bench/authoring/.out
#
# For each tasks/*.txt it runs two arms (base, craw) K times, capturing
# total_cost_usd / duration_ms / num_turns / token usage from `claude -p --output-format json`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/.out"
K="${CRAW_BENCH_K:-1}"
TOOLS="Read,Write,Edit,Bash"
rm -rf "$OUT"; mkdir -p "$OUT"

printf '%-22s %-6s %-4s %10s %8s %6s\n' task arm run cost_usd dur_s turns

for taskfile in "$HERE"/tasks/*.txt; do
  task="$(basename "${taskfile%.txt}")"
  for arm in base craw; do
    for k in $(seq 1 "$K"); do
      wd="$OUT/$task/$arm/run$k"; mkdir -p "$wd"
      prompt="$(cat "$taskfile")"
      if [ "$arm" = craw ]; then
        prompt="$prompt$(REPO="$REPO" envsubst < "$HERE/craw_suffix.txt" 2>/dev/null || sed "s#\$REPO#$REPO#g" "$HERE/craw_suffix.txt")"
      fi
      ( cd "$wd" && claude -p "$prompt" --allowedTools "$TOOLS" --output-format json \
          > result.json 2> err.log ) || true
      read -r cost dur turns < <(python3 - "$wd/result.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(f"{d.get('total_cost_usd',0):.4f} {d.get('duration_ms',0)/1000:.1f} {d.get('num_turns',0)}")
except Exception: print("NA NA NA")
PY
)
      printf '%-22s %-6s %-4s %10s %8s %6s\n' "$task" "$arm" "$k" "$cost" "$dur" "$turns"
    done
  done
done
echo
echo "Outputs under $OUT — score quality with:  python3 $HERE/evaluate.py $OUT"
