# Dirty preservation reconciliation

Reconciled Tyler's uncommitted live patch onto candidate `8f734f1f53` in this isolated worktree.

- `docker/SOUL.md`: retained the candidate's concise Hermes personality and adopted the live patch's authorized credential custody wording and exact-block attribution.
- `gateway/platforms/base.py`: retained the candidate's adapter guards, media merging, context references, and staged watermark handling. Shared media classification now drives selective transcript-cache invalidation; caption-only merges retain transcripts and the echo ledger survives new audio.
- `gateway/run.py`: retained the extracted facade, media classification refinements, and hygiene state structures. Ported the live patch's actual behavioral changes into `run_busy.py` and `run_inbound.py`, avoiding restoration of obsolete monolithic implementations.
- Busy queue admission now prepares voice transcripts before acknowledgements, including priority-path queues and steer/interrupt demotions. Concurrent arrival/drain callers share transcription; captions recompose against cached transcripts. Existing buttons, queue/steer/redirect/interrupt/stop routing, active-session acknowledgements, text debounce, FIFO boundaries, authorization, and per-profile mode resolution remain intact.
- Media-merge admission now returns `True`, matching successful FIFO admission. This was necessary to prevent accepted merged voice notes from skipping arrival transcription.
- Preserved all incoming tests. Adapted the caption-merge test to explicitly select immediate admission (`force_busy_ack=True`), keeping the candidate's ordinary text debounce behavior and existing debounce tests. Updated relay tests to import moved media helpers from their defining module.

Validation:

- `git diff --check` and staged whitespace check: passed; no unresolved index entries.
- `python3 -m py_compile` for the four affected gateway modules and both affected test modules: passed.
- Requested three-file `scripts/run_tests.sh ... -q` command: **62 passed**.
- Six related suites covering relay media, compression demotion, multiplex busy modes, session races, STT echoes, and Telegram audio/voice classification: **54 passed**.

Clawpatch (`doctor`, `init`, `map`, Codex review, `report`) completed. It reported two pre-existing issues in unchanged files, left outside this preservation scope: profile context loss in `gateway/run_goals.py:324` (`fnd_sig-feat-library-1c1a2912a0-aec8_af43df4431`) and uncaught reply-index read errors in `gateway/rich_sent_store.py:24` (`fnd_sig-feat-library-1c1a2912a0-ef84_31afaee7d6`). Temporary Clawpatch inventory was removed; no automated repair was run.

Validation uses isolated test state and mocked transport/STT boundaries; no live service or provider end-to-end test was performed. No reset/abort, remote changes, push, service restart, or OpenRouter use.
