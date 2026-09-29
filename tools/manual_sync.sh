#!/usr/bin/env bash
# Manual sync equivalent to .github/workflows/mirror.yaml (with --push).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DOWNSTREAM="${DOWNSTREAM:-git@github.com:tier4/pilot_auto_visualization.git}"
WORK="${WORK:-/tmp/pilot_auto_visualization-sync}"
PUSH="${PUSH:-1}"

mkdir -p "$WORK"
export PYTHONPATH="$ROOT/tools${PYTHONPATH:+:$PYTHONPATH}"

echo "==> validate"
tools/sync_config.py validate

mapfile -t SOURCES < <(tools/mirror.py list-sources | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)), end="")')
mapfile -t COMBINED < <(tools/mirror.py list-combined | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)), end="")')

push_flag=()
if [[ "$PUSH" == "1" ]]; then
  push_flag=(--push)
  echo "==> live sync (will push to $DOWNSTREAM)"
else
  echo "==> dry run (no push)"
fi

for source in "${SOURCES[@]}"; do
  echo
  echo "==> mirror $source"
  tools/mirror.py mirror "$source" --work "$WORK/mirror" --downstream "$DOWNSTREAM" "${push_flag[@]+"${push_flag[@]}"}"
done

for target in "${COMBINED[@]}"; do
  echo
  echo "==> combine $target"
  if [[ "$PUSH" == "1" ]]; then
    tools/mirror.py combine "$target" --work "$WORK/combine" --downstream "$DOWNSTREAM" --verify --push
  else
    tools/mirror.py combine "$target" --work "$WORK/combine" --downstream "$DOWNSTREAM" --verify --allow-missing-members
  fi
done

echo
echo "==> done"
