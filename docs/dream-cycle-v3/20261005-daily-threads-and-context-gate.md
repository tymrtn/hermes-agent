# Dream Cycle v3: daily threads and the no-new-context gate

Follows `20261004-daily-brief-and-reconciled-handoff.md`.

## Two failures this fixes

1. **No priority, group or code in the brief.** The handoff had no structured thread state, so the
   brief listed loose bullets cut at 160 characters, sometimes mid-word.
2. **The brief repeated every night.** Each run compressed the conversation again and sent a new
   brief even when nothing had been said since the last one.

## Daily Threads: the handoff section

The scheduled (reconciled) summarization asks for one more section. Manual `/compress` and
auto-compression are unchanged.

```
## Daily Threads
- P1-group/task — current state; next: next action
  - waiting externally: who or what, next check date
- proposed P2-group/task — ...
- unassigned: short title — ...
```

- The dependency line is optional, at most one per thread. Kinds: `blocked`, `needs help`,
  `needs authorization`, `waiting externally`.
- A thread's identity is `key` (`group/task`). The priority is shown in the code, so a
  re-prioritized thread keeps its key.
- Next actions and dependency lines are asked for as 4–10-word imperatives, with reasons and
  consequences kept in the state.
- `verify_codes` (`agent/context_compressor_daily_threads.py`) runs before the summary is stored.
  It rewrites a code line as `proposed` unless the user wrote that exact code, or the previous
  stored handoff already carried it as established. The summarizer's own text, compaction
  handoffs and mirrored `[Cron delivery:` rows do not count as the user. `P?` is always proposed.
- Replay repair merges a mirrored brief into the user rows around it. The scheduled path therefore
  verifies against the stored, unmerged rows (`reconcile_handoff_with_tail(compressor,
  stored_rows)`), so the brief's own codes never read as the user's and a reply typed straight
  after the brief still counts. Any other compression path cuts a merged row at the
  `[Cron delivery:` line and drops the rest of that row.

## The delivered layout

Each thread is exactly two lines, with a blank line between threads:

```
🟠 P1-startups/expatriator
⏳ Madrid portal support reply, check Tue 7 Oct
```

- Line 1 is the priority dot and the established code. Proposed codes sit under "**Proposed** ·
  codes and priorities not agreed yet" with a `•` and no dot. Uncoded threads sit under "**No code
  yet**" with their title.
- Line 2 is the state symbol and the next action. The symbols are ⛔ blocked, 🙋 needs help,
  🔐 needs authorization and ⏳ waiting externally, each showing the dependency line. ▶️ means no
  dependency and shows the `next:` action. Registered threads use 💤 for `stale`.
- `describe` (`dream_cycle_v3/daily_brief.py`) enforces both bounds without a model call. The
  secret scan reads the text as recorded and again as finally displayed (capitalized, with any
  suffix), and a hit at either point leaves the line empty. Between the two scans the text becomes
  plain: link text is kept, bare URLs are dropped, and emphasis and code marks are removed, so no
  cut can leave Markdown open. Parenthetical asides are dropped. A longer text keeps its longest run of
  leading clauses, ending at `;`, `:`, `,`, a spaced dash, "so that", "because", "which", "while",
  "unless", "whereas" or "in order to", provided that run is at least four words. Otherwise it
  keeps ten whole words, drops a trailing connective and ends with `…`. A text under four words is
  not padded with invented work; it gets " (no further detail recorded)".
- Threads of one group are listed together. Groups are ordered by their most urgent thread. There
  are no group headings, since the group is already the code's first segment.
- The state text is kept in the handoff and is not displayed.

## Field contract

Gateway highlight `daily_threads`, present only when the section exists:

| field | meaning |
|---|---|
| `threads[]` | at most 12 |
| `threads[].standing` | `established`, `proposed` or `uncoded` |
| `threads[].code`, `priority`, `key`, `group` | coded threads only; `code == priority + "-" + key`; `group` is the first segment of `key` |
| `threads[].title` | uncoded threads only |
| `threads[].state`, `next` | free text, cut at a word boundary to 160 characters |
| `threads[].dependency` | optional `{kind, detail}`; `kind` is `blocked`, `needs_help`, `needs_authorization` or `waiting_externally` |
| `unformatted[]`, `unformatted_total` | section lines outside the grammar (at most 3 shown) and their count |

The client (`dream_cycle_v3/conversation_compression.py`) re-validates every field. Enums go
through `sanitize_enum`, codes through `sanitize_identifier`, and text through `sanitize_text` then
`clip_words`. A coded thread whose code does not equal `priority-key` is dropped.

Other conversation-record fields:

- `claim` may now be `unchanged`, meaning the gateway confirmed nothing new was said since its last
  applied compression.
- `activity: {settled, fingerprint}`. Settled means every eligible session answered `applied`,
  `already_applied` or `unchanged` with a fingerprint. Anything else (pending, deferred, failed,
  no gateway) is unsettled.
- `highlights.daily_threads` merges every settled session's threads. A conversation with nothing
  new keeps its open threads in the brief when another one was compressed tonight.
- `highlights.unthreaded: {conversations, items[]}` appears beside `daily_threads` when some
  settled sessions have no daily-thread section. The brief lists their open items under "⚠️ N
  conversation(s) had no daily-thread section, so … items are unprioritized". (One conversation
  per night is the default, `--conversation-compression-limit 1`.)

The cycle report gains `registered_threads: {threads[], total}`, a projection of the operator
registry: id, project name, title, state, next action, blocker, follow-up date. The registry
contract has no priority or group field, so the brief labels these threads "no priority recorded".

## The gate

There are two places where the run stops early.

1. **Gateway, before any reservation or model call.** `compress-session` fingerprints the stored
   rows (raw, no alternation repair), excluding compaction handoffs and mirrored cron deliveries.
   If the routing entry's last `applied` marker carries the same fingerprint, the answer is
   `unchanged`. The marker's fingerprint is taken from the rows actually persisted after the
   compression. A same-cycle retry answers `already_applied` with the fingerprint of the rows as
   they stand now, so anything said since that compression is still new context. Both answers are
   checked again once the conversation is reserved, so a request that read the marker before a
   concurrent one stamped it does not summarize a second time.
2. **Runtime, before delivery.** `context_gate.decide` hashes the conversation fingerprint together
   with a content digest of the registry, the thread seed and the tracker snapshots. If that equals
   the last `runtime_context_checkpoint` for the profile and mode, and no compression ran this time,
   the run is skipped. `dream_cycle_v3 run` then prints `{"wakeAgent": false}` to stdout and the
   status line to stderr. Cron reads that last line as silence (`cron/scheduler_prompt.py::
   _parse_wake_gate`) for no-agent and agent jobs alike.

The checkpoint is recorded under the runtime lock only after the brief was written and flushed, and
only when the conversation state was settled. Checkpoints are kept per profile, mode and output, so
a status line that went out (`--output status`) never silences the first brief. A failed, deferred, pending or unavailable run is
always delivered and never checkpointed, so the next run compares against the last brief that went
out.

## Fallbacks

- **Handoff without the section** (anything compressed before the gateway restart): the brief opens
  with "⚠️ No priorities, groups or codes: this handoff has no daily-thread section, so its items are
  unprioritized." Old 160-character cuts are trimmed back to the last whole word.
- **No established codes:** "⚠️ No agreed P#-group/task codes yet, so nothing here is prioritized."
- **Proposed and uncoded threads** get their own labelled blocks with no priority dot.
- **Reports from before this change** have no `registered_threads`. The brief falls back to the
  carry-forward rows.

## Activation

- The renderer, runtime gate and CLI load per cron run. They take effect on the next run, with no
  restart.
- The gateway side (`Daily Threads`, `verify_codes`, the `unchanged` answer) needs that profile's
  gateway restarted. Until then the gateway never answers `unchanged`, the conversation stays
  unsettled, and every night delivers, as it does today.
- The first night after the restart compresses once more to record a fingerprint. Repeat nights are
  silent from then on.
- The schedule does not change.

## Limits

- Collected read-root sources are not part of the gate. They are ingested every night but never
  rendered in the brief.
- Time-driven carry-forward (a thread going stale with nothing said) does not trigger a brief.
- Telegram delivery happens after the script exits. If cron cannot send, the checkpoint already
  exists, and the next run is silent unless something new arrives.
- A compression the gateway refuses because it would grow the context stays retryable, so it is
  attempted again every night.
- A priority change only counts as established when the user writes the full new code.
- Outside the scheduled path, a code the user writes in a reply merged after a mirrored brief is
  not counted until it appears in a row of its own.
- A conversation too short to compress (`deferred`, `too_short`) counts as settled. The gateway
  stamps no marker for it and checks it again every night. Only a delivery with identical content
  is silenced.
- `verify_codes` trusts codes that the previous stored handoff carried as established. Every
  handoff written since this change was verified before it was stored. Codes from a handoff written
  by a plugin context engine, or one with a `## Daily Threads` section from before this change,
  would be trusted without that check. No provenance record exists to tell them apart. Nagatha's
  2026-10-04 handoff has no such section.
- The ▶️ and 💤 symbols are this implementation's choice. The skill names symbols only for the four
  dependency states.
