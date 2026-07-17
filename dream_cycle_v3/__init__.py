"""Dream Cycle v3 — Phases 0-3: deterministic collection, continuity store,
promotion adapters, and the wake/retrieval read layer (wake.py, lookup.py).

Design contract: dream-cycle-v3-plan.md / dream-cycle-v3-schemas.json
(copies of the machine contract live in dream_cycle_v3/contracts/).

Boundaries: read-only toward every external system; the package writes only
to caller-selected roots (manifest dir, continuity DB, report paths, and — in
Phase 2 — explicitly passed destination homes). Nothing here resolves a live
profile memory/skills/docs path on its own; destination adapters require the
caller to hand them a home directory, so live state can never be a default.
"""

COLLECTOR_VERSION = "3.0.1-phase1"  # 3.0.1: full-file streaming fingerprints + transcript suppression
STORE_SCHEMA_TARGET = 4  # 4: projects.project_id registry-grammar guard
MANIFEST_SCHEMA_VERSION = 1
CONTRACT_SCHEMA_VERSION = 1
