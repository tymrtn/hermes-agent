#!/usr/bin/env bash
# Dream Cycle v3 live continuity cron shim for the Nagatha profile.
#
# Runs the deterministic Phase 4 collection/store/retrieval cycle against the
# profile-bound continuity home consumed by gateway wake and continuity_lookup.
# The runtime has no destination-promotion path and never restarts a gateway.
set -euo pipefail

HERMES_AGENT_REPO="$HOME/.hermes/hermes-agent"
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$HERMES_AGENT_REPO/.venv/bin/python" ]; then
  PY="$HERMES_AGENT_REPO/.venv/bin/python"
else
  PY="$HERMES_AGENT_REPO/venv/bin/python"
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "dream-cycle-v3 live cron: python not executable at $PY" >&2
  exit 64
fi
WRAPPER="$HERMES_AGENT_REPO/scripts/dream_cycle_v3_run.sh"
if [ ! -x "$WRAPPER" ]; then
  echo "dream-cycle-v3 live cron: wrapper not executable at $WRAPPER" >&2
  exit 64
fi

read -r DC3_DATE TODAY_UTC <<EOF
$("$PY" -c 'from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
print((now - timedelta(days=1)).date().isoformat(), now.date().isoformat())')
EOF

export DC3_PROFILE="nagatha"
export DC3_OWNER="nagatha"
export DC3_ROOTS="v2-state=$HOME/.hermes/dream-cycle/state/nagatha v2-runs=$HOME/.hermes/dream-cycle/runs sessions=$HOME/.hermes/profiles/nagatha/sessions"
unset DC3_SHADOW_ROOT
export DC3_V3_ROOT="$HOME/.hermes/profiles/nagatha/dream-cycle-v3"
export DC3_DATE
export DC3_WINDOW_START="${DC3_DATE}T00:00:00+00:00"
export DC3_WINDOW_END="${TODAY_UTC}T00:00:00+00:00"
export DC3_REGISTRY="$HOME/.hermes/dream-cycle/v3-config/projects.json"
export DC3_THREADS="$HOME/.hermes/dream-cycle/v3-config/threads.json"
export DC3_MIGRATE_V2_ROOTS="v2-state v2-runs"

cd "$HERMES_AGENT_REPO"
exec "$WRAPPER"
