#!/usr/bin/env bash
# Dream Cycle v3 Phase 4 SHADOW cron shim for the Nagatha profile.
#
# Referenced by the tracked, INACTIVE cron definition at
# docs/dream-cycle-v3/nagatha-shadow-cron.job.json and installed into
# ~/.hermes/profiles/nagatha/scripts/ per
# docs/dream-cycle-v3/nagatha-shadow-publication-checklist.md.
#
# This shim only sets explicit DC3_* inputs and execs the tracked no-agent
# wrapper (scripts/dream_cycle_v3_run.sh) in SHADOW mode: all output is
# isolated under the shadow root, the runtime performs collection/store/
# retrieval only, and there is no interaction with the legacy v2 job, the
# gateway lifecycle, or any live destination.
#
# Every path below is the real intended Nagatha location, stated
# literally. The ONLY computed values are yesterday's one-day UTC window
# (both dates come from a single clock read so the window can never span
# a midnight race).
set -euo pipefail

PY="${PYTHON:-python3}"

HERMES_AGENT_REPO="$HOME/.hermes/hermes-agent"
WRAPPER="$HERMES_AGENT_REPO/scripts/dream_cycle_v3_run.sh"
if [ ! -x "$WRAPPER" ]; then
  echo "dream-cycle-v3 shadow cron: wrapper not executable at $WRAPPER" >&2
  exit 64
fi

read -r DC3_DATE TODAY_UTC <<EOF
$("$PY" -c 'from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
print((now - timedelta(days=1)).date().isoformat(), now.date().isoformat())')
EOF

# Real current sources (observed 2026-07-13): the v2 dream-cycle state
# and dated run artifacts, plus the Nagatha session logs. Only the two v2
# artifact roots are quarantine-migrated; sessions are collected but never
# migrated.
export DC3_PROFILE="nagatha"
export DC3_OWNER="nagatha"
export DC3_ROOTS="v2-state=$HOME/.hermes/dream-cycle/state/nagatha v2-runs=$HOME/.hermes/dream-cycle/runs sessions=$HOME/.hermes/profiles/nagatha/sessions"
export DC3_SHADOW_ROOT="$HOME/.hermes/dream-cycle/v3-shadow"
export DC3_DATE
export DC3_WINDOW_START="${DC3_DATE}T00:00:00+00:00"
export DC3_WINDOW_END="${TODAY_UTC}T00:00:00+00:00"
export DC3_REGISTRY="$HOME/.hermes/dream-cycle/v3-config/projects.json"
export DC3_THREADS="$HOME/.hermes/dream-cycle/v3-config/threads.json"
export DC3_MIGRATE_V2_ROOTS="v2-state v2-runs"

cd "$HERMES_AGENT_REPO"
exec "$WRAPPER"
