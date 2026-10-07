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
  It rewrites a code line as `proposed` unless the user agreed to that exact code, or the code is
  in the verified set the last applied marker recorded. Agreeing means a user sentence that names
  the code with an approving word ("use", "track", "agreed", "yes", ...) and nothing that asks,
  refuses, hedges, gives an example or defers the decision to later. Quoted text (code spans,
  quotation marks, `>` lines) is dropped first, so a code pasted as an example is not approval.
- A code the user never retypes can still be established, bound to the proposition it answers. The
  assistant turn immediately before must OFFER exactly one code in words ("proposing", "shall we
  use", "let's use"); naming one while explaining, illustrating or asking about it offers nothing,
  and two codes leave "use that" pointing nowhere. A question mark does not disqualify an offer —
  "Shall we use P2-ops/logs?" IS the offer — but a turn that CLOSES on a question naming no code
  offers nothing to answer: "Proposing P2-ops/logs. Are the backups healthy?" followed by "Yes,
  they are." establishes nothing, because the reply is about the backups. **A stored compaction
  handoff never supplies an antecedent** (`is_compaction_summary_message`, by metadata key or by
  content prefix): it is the summarizer's own text replayed as an assistant row, so a `proposed
  P2-x/y` line it wrote would otherwise become provenance for consent the user never gave.
  Provenance travels in the gateway's marker (`carried`), never in the transcript.
- The reply itself must agree to the proposal rather than to a subject of its own: it either points
  at what was offered or carries nothing else at all ("Yes.", "Agreed, please."). Pointing means a
  referent that IS the thing agreed to — followed by nothing, or by a prepositional/conjunctive
  adjunct ("yes, use that", "go with it", "use that for the rotation work"). A referent carrying a
  complement of its own does not point: "Keep it running." is about the log volume. A **possessive
  is never a pointer on its own**, because it modifies the noun after it — "Keep your backups
  running." and "Yes, use your usual backup settings." agree to the backups; only an explicit
  "your suggestion"/"your proposal"/"your wording" refers. The antecedent is consumed by the turn
  that answers it, so a later approval cannot reach back past it. The summarizer's own text,
  compaction handoffs and mirrored `[Cron delivery:` rows do not count as the user. `P?` is always
  proposed.
- Provenance: the routing entry's marker records the codes that came out of verification as
  `daily_threads: {version: 1, established: [...]}`. That set, and nothing read from the handoff's
  text, is what carries to the next pass and what the gateway reports as established. A handoff
  with no such record (written before this change, or by a plugin context engine) establishes no
  code until the user agrees to it again.
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
- The words are the summarizer's: the existing scheduled pass is asked for each `next:` and
  dependency line as one 4–10-word imperative. `describe` (`dream_cycle_v3/daily_brief.py`) only
  validates, with no model call and no rewriting: the secret scan reads the text as recorded and as
  displayed, the text becomes plain (link text kept, bare URLs, emphasis and code marks removed),
  and it must then be 4–10 whole words. A text that fails any check is not clipped or padded; the
  line reads "⚠️ No 4–10-word next step recorded" (after the state symbol when there is one). That
  is what older handoffs and registry rows without a usable next action show.
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
- `activity: {settled, fingerprint, sessions}`. Settled means every eligible session answered
  `applied`, `already_applied` or `unchanged` with a fingerprint. Anything else (pending, deferred,
  failed, no gateway) is unsettled. `sessions` is `{session_ref: fingerprint}` for the conversations
  THIS answer covers; `fingerprint` digests that set alone and is report evidence. The gate does not
  compare it — see "Conversation" under the gate.
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

Each night starts with a lightweight poll (`context_gate.decide_poll`, `runtime._poll`). It writes
no manifest, report or store row other than delivery bookkeeping, and makes no model call. Only its
`cycle` answer runs the substantive cycle (manifest, store pass, compression, report, brief).

What the poll reads, i.e. what counts as new context:

- **Conversation.** A `compress-session` dry run, answered by the gateway from a read-only pass:
  the fingerprint of the stored rows (raw, no alternation repair), excluding compaction handoffs
  and mirrored cron deliveries, so the brief's own echo in the transcript is not new. If the last
  applied marker carries the same fingerprint the answer is `unchanged`; otherwise `eligible`.
  Anything that does not pin the state (no gateway, pending, deferred, failed) is unsettled and
  runs a cycle.
  A rewrite nobody added a turn to is not an exchange either. Manual `/compress` and automatic
  (hygiene) compression stamp no cycle marker, so both carry the marker's fingerprint across their
  own commit (`carry_fingerprint_across_rewrite`): the marker keeps the LOGICAL fingerprint the last
  delivered brief described while the physical one follows the rewrite, and the gateway reports the
  logical one on `unchanged` and on `already_applied` alike. Automatic compression carries from two
  sites, because it commits from two: the adoption step, and the done-callback of a watermark-fenced
  worker that finished AFTER its turn hold was released (`_hmwa_hygiene_on_turn_hold`).
  The carry needs both of its guards. The pre-rewrite rows must still have matched the marker
  exactly, AND what the rewrite kept must be a contiguous run of what was there, in stored order
  (`_added_a_turn`): a watermark-fenced commit CLONES rows that arrived while it ran into the tail it
  keeps, so the pre-rewrite rows can match while the result holds talk nobody has been told about.
  Either guard failing leaves the stale fingerprint in place and the conversation eligible.
  That guard compares ORDERED row identities (`[role, timestamp, text]` — the timestamp is what the
  store's own exact-clone index keys on), never a set of `(role, text)` pairs, and deliberately not
  the fingerprint's own `[role, text]` projection. `activity_fingerprint` hashes an ordered list, so
  a user and an assistant repeating an exchange word for word moves it; a set of pairs would have
  found every pair already present and carried the fingerprint over a night the user talked through.
  Occurrence and order both count, and an unreadable `before` or an `after` longer than it finds no
  run and refuses — the carry is the only thing that can silence a night, so it fails towards an
  extra brief.
  **Membership is durable, not reconstructed each night.** Which conversations an answer covers
  depends on the gateway's activity window, its retention cap (`MAX_RETAINED_ENTRIES`), the
  per-night `limit`, and whether housekeeping has swept an idle routing entry — none of which is
  new context. So the gate keeps `{session_ref: fingerprint}` beside every output it records and
  merges tonight's `activity.sessions` over it: a conversation tonight's answer is silent about keeps
  the state it was last known in. Only an answer that really moved moves the digest.
  The baseline is the most recent output a reader is owed, or has been sent (`baseline_sessions`): an
  unresolved pending brief, else the delivered checkpoint. An owed brief must be judged against the
  membership IT was built from — the question there is "is this still the right brief" — or a
  conversation dropping out of the answer makes tonight's state differ from the pending one's, the
  poll rebuilds instead of resending, and the pending row never resolves. Serving as the baseline is
  not acknowledgement: only cron's ledger resolves a pending delivery.
- **Canonical records.** A content digest of the registry, the thread seed and the tracker
  snapshots. A file rewritten with the same entries digests the same.
- **Consumed sources.** The migrated roots' (`--migrate-v2-root`) candidates the store holds, plus
  tonight's window of them collected read-only with the cycle's own selection
  (`runtime.consumed_sources`), as ids in the content digest. Identity is the source path plus its
  content fingerprint, so a file re-stamped or rewritten with the same bytes is not new. A source
  a cycle ingested but never delivered a brief for (the cycle failed after ingesting) still differs
  from the last delivered state, so the next night runs again. Roots that are only recorded (the
  sessions root, which is the conversation) are not counted, and neither are the cycle's own
  reports, manifests and store rows. A cycle that ran for sources says so: "📥 N new source
  file(s) quarantined for review".

Delivery is acknowledged by cron, never assumed. Writing to stdout is not delivery: the scheduler
sends after the script exits. A cron run carries `HERMES_CRON_EXECUTION_ID` (its row in the
executions ledger). The emitted text is kept under `v3_root/deliveries/<sha256>.txt` and recorded
as `runtime_delivery_pending` with that id. The next poll reads the row's `delivery_outcome`
(`queued` follows the delivery queue's row):

| ledger says | poll does |
|---|---|
| `delivered` | records `runtime_context_checkpoint`; an unchanged night is then silent |
| still running or queued | an unchanged night waits (silent) |
| `failed`, `suppressed`, `not_configured`, unknown | an unchanged night writes the kept text again, unchanged, under the new execution id; no cycle, no compression |
| anything | changed context runs a new cycle, whose brief supersedes the pending one |

Silence is `{"wakeAgent": false}` on stdout with the status (or poll) line on stderr; cron reads it
as silence for no-agent and agent jobs alike (`cron/scheduler_prompt.py::_parse_wake_gate`).
Checkpoints are kept per profile, mode and output. A run by hand has no execution id, so it records
nothing and silences nothing. Programmatic `run_cycle` calls without `emit` (replay, tests) always
run the full cycle.

The gateway keeps its own early stop as well: a non-dry `compress-session` on an `unchanged`
conversation answers without a reservation or a model call, and both `unchanged` and
`already_applied` are re-checked once the conversation is reserved.

## Fallbacks

- **Handoff without the section** (anything compressed before the gateway restart): the brief opens
  with "⚠️ No priorities, groups or codes: this handoff has no daily-thread section, so its items are
  unprioritized." Old 160-character cuts are trimmed back to the last whole word.
- **No established codes:** "⚠️ No agreed P#-group/task codes yet, so nothing here is prioritized."
- **Proposed and uncoded threads** get their own labelled blocks with no priority dot.
- **Reports from before this change** have no `registered_threads`. The brief falls back to the
  carry-forward rows.

## Activation

- The renderer, poll and CLI load per cron run, so they take effect on the next run.
- Everything else needs that profile's gateway restarted, since the gateway also runs the cron
  ticker: the read-only dry run, `unchanged`, Daily Threads with provenance, and the
  `HERMES_CRON_EXECUTION_ID` export. Until then a cron run has no execution id, so nothing is
  checkpointed and every night runs a cycle and delivers, as it does today. No night is lost.
- The first night after the restart compresses once more to record a fingerprint and a verified
  code set. A carried handoff's codes show as proposed until the user agrees to them.
- The schedule does not change.

## Limits

- Time-driven carry-forward (a thread going stale with nothing said) does not trigger a brief.
- A resent brief is the original night's brief, its date included: it is the one that never
  arrived. When the kept text is gone or fails its digest, the cycle runs again instead.
- When the context changed since an undelivered brief, the new cycle's brief replaces it. The
  undelivered one is not sent separately.
- A compression the gateway refuses because it would grow the context stays retryable, so it is
  attempted again every night.
- Approval is lexical. An approving word with no question, refusal, hedge, example or deferral word
  in the same sentence counts. Wording outside those lists errs towards `proposed`. Approval by
  reference is bound to the proposition only as far as word lists reach: it requires a single
  unambiguous offer in the turn immediately before (never a compaction handoff), that turn not
  closing on an unrelated question, and a reply whose referent is the thing agreed to rather than a
  word standing in front of the sentence's own subject. Anything else stays `proposed` — the failure
  direction is deliberate, since a `proposed` code costs the user one sentence and an invented
  approval puts words in their mouth.
- The gate's conversation membership only ever grows. A conversation deleted for good keeps its last
  fingerprint in the checkpoint; it silences nothing (a replacement conversation is a new
  `session_ref`), but the map carries one 64-hex digest per conversation the cycle has ever
  delivered a brief for. Bounded by the number of chats, not by time.
- A priority change only counts as established when the user agrees to the full new code.
- Outside the scheduled path, a code the user writes in a reply merged after a mirrored brief is
  not counted until it appears in a row of its own.
- A conversation too short to compress (`deferred`, `too_short`) counts as settled. The gateway
  stamps no marker for it and checks it again every night.
- The registry contract has no priority or group field, so registered threads stay "no priority
  recorded" until it does.
- The ▶️ and 💤 symbols are this implementation's choice. The skill names symbols only for the four
  dependency states.
