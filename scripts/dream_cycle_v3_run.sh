#!/usr/bin/env bash
# Tracked no-agent cron wrapper for the Dream Cycle v3 Phase 4 runtime.
#
# Builds an explicit `python -m dream_cycle_v3 run` invocation from
# environment variables — no ambient discovery, no LLM calls, no
# credentials, no gateway restart. With DC3_CONVERSATION_COMPRESSION=1 it
# additionally asks the already-running gateway to compress the conversation
# through its own /compress path; the aux-model call happens there, under that
# gateway's credentials, and a gateway that does not answer is reported as
# "not applied" rather than worked around. On success stdout is the human daily
# brief rendered from the cycle's own records (no model call) and the one exact
# machine status line goes to stderr; DC3_OUTPUT=status restores the status line
# on stdout. Any failure exits nonzero with a concise safe message on stderr.
#
# Required env:
#   DC3_PROFILE DC3_OWNER DC3_ROOTS DC3_WINDOW_START DC3_WINDOW_END DC3_DATE
#   and exactly one of DC3_V3_ROOT | DC3_SHADOW_ROOT
# DC3_ROOTS is a space-separated list of KEY=PATH read roots.
# Optional env:
#   DC3_AS_OF DC3_REGISTRY DC3_THREADS DC3_KANBAN_DB DC3_KANBAN_BOARD
#   DC3_TODOIST_EXPORT DC3_GITHUB_REPO DC3_GITHUB_AVAILABLE=1
#   DC3_KANBAN_ROOT DC3_PROJECTS_HOME DC3_MIGRATE_V2_ROOTS (space-separated
#   root keys) DC3_STALE_AFTER_DAYS DC3_SMOKE_MESSAGE
#   DC3_SMOKE_EXPECT_PROJECT DC3_SMOKE_REQUIRE_THREAD=1 PYTHON
#   DC3_CONVERSATION_COMPRESSION=1 (ask the live gateway to run one REAL
#   conversation compression over its own control socket; still no LLM call
#   from THIS process) DC3_CONVERSATION_COMPRESSION_LIMIT DC3_GATEWAY_HOME
#   DC3_OUTPUT=brief|status (default brief)
# Extra CLI arguments pass through verbatim: dream_cycle_v3_run.sh [ARGS...]
set -euo pipefail

fail() {
  echo "dream-cycle-v3 run: $1" >&2
  exit 64
}

require() {
  if [ -z "${!1:-}" ]; then
    fail "missing required env $1"
  fi
}

require DC3_PROFILE
require DC3_OWNER
require DC3_ROOTS
require DC3_WINDOW_START
require DC3_WINDOW_END
require DC3_DATE
if [ -z "${DC3_V3_ROOT:-}" ] && [ -z "${DC3_SHADOW_ROOT:-}" ]; then
  fail "set exactly one of DC3_V3_ROOT or DC3_SHADOW_ROOT"
fi
if [ -n "${DC3_V3_ROOT:-}" ] && [ -n "${DC3_SHADOW_ROOT:-}" ]; then
  fail "set exactly one of DC3_V3_ROOT or DC3_SHADOW_ROOT"
fi

args=(run
  --output "${DC3_OUTPUT:-brief}"
  --profile "$DC3_PROFILE"
  --owner "$DC3_OWNER"
  --window-start "$DC3_WINDOW_START"
  --window-end "$DC3_WINDOW_END"
  --date "$DC3_DATE")

for root in $DC3_ROOTS; do
  args+=(--root "$root")
done

add_opt() {
  local value="${!1:-}"
  if [ -n "$value" ]; then
    args+=("$2" "$value")
  fi
}

add_opt DC3_V3_ROOT --v3-root
add_opt DC3_SHADOW_ROOT --shadow
add_opt DC3_AS_OF --as-of
add_opt DC3_REGISTRY --registry
add_opt DC3_THREADS --threads
add_opt DC3_KANBAN_DB --kanban-db
add_opt DC3_KANBAN_BOARD --kanban-board
add_opt DC3_TODOIST_EXPORT --todoist-export
add_opt DC3_GITHUB_REPO --github-repo
add_opt DC3_KANBAN_ROOT --kanban-root
add_opt DC3_PROJECTS_HOME --projects-home
add_opt DC3_STALE_AFTER_DAYS --stale-after-days
add_opt DC3_SMOKE_MESSAGE --smoke-message
add_opt DC3_SMOKE_EXPECT_PROJECT --smoke-expect-project
add_opt DC3_CONVERSATION_COMPRESSION_LIMIT --conversation-compression-limit
add_opt DC3_GATEWAY_HOME --gateway-home

if [ -n "${DC3_MIGRATE_V2_ROOTS:-}" ]; then
  for key in $DC3_MIGRATE_V2_ROOTS; do
    args+=(--migrate-v2-root "$key")
  done
fi
if [ "${DC3_GITHUB_AVAILABLE:-0}" = "1" ]; then
  args+=(--github-available)
fi
if [ "${DC3_SMOKE_REQUIRE_THREAD:-0}" = "1" ]; then
  args+=(--smoke-require-thread)
fi
if [ "${DC3_CONVERSATION_COMPRESSION:-0}" = "1" ]; then
  args+=(--conversation-compression)
fi

exec "${PYTHON:-python3}" -m dream_cycle_v3 "${args[@]}" "$@"
