# Claude Proxy Fork Maintenance

Upstream PR #10576 was closed by policy, not because the fix was wrong. Keep the Claude Max/OAuth compatibility workaround in Tyler's fork.

## Branch

- Fork: `tymrtn/hermes-agent`
- Branch: `tyler/claude-proxy-fork`
- Base at last verification: `origin/main`
- Patch commits:
  - `e90d725ab` — sanitizer from closed upstream PR #10576
  - `05e418355` — fork-only hardening for gateway/media triggers, skill-catalogue triggers, and OAuth output cap

## What the fork preserves

- Claude OAuth system-prompt sanitizer for request-shape misclassification.
- Gateway `MEDIA:` trigger rewrite.
- Skill-catalogue trigger rewrites for red-team adjacent terms.
- Claude OAuth-only `max_tokens` cap at `32_000`; API-key Anthropic traffic keeps documented ceilings.
- Regression coverage in `tests/agent/test_anthropic_oauth_local_fork.py`.

## Safe refresh procedure

```bash
cd /tmp/hermes-claude-proxy-fork
git fetch origin main
git fetch tymrtn tyler/claude-proxy-fork
git checkout tyler/claude-proxy-fork
git rebase origin/main
pytest -q -o "addopts=-m 'not integration'" \
  tests/agent/test_anthropic_oauth_local_fork.py \
  tests/test_ctx_halving_fix.py::TestBuildAnthropicKwargsClamping
git push --force-with-lease tymrtn tyler/claude-proxy-fork
```

## Verification command used

```bash
pytest -q -o "addopts=-m 'not integration'" \
  tests/agent/test_anthropic_oauth_local_fork.py \
  tests/test_ctx_halving_fix.py::TestBuildAnthropicKwargsClamping
```

Result on 2026-05-16: `8 passed in 1.21s`.

## Notes

Do not reopen the sanitizer upstream unless policy changes. Teknium explicitly directed this class of workaround to local forks. Upstream can have the academic purity; we need the bot to answer Telegram.
