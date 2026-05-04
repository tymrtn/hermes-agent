---
name: read-file-prefix-corruption-repair
description: Detect and repair text files corrupted when an agent saves `read_file` tool output back to disk. Damage looks like every line prefixed with `     N|content`, sometimes nested twice. Use when documentation or config files show line-number-shaped left margins after agent edits.
tags: [hermes, file-corruption, agent-hygiene, repair, markdown]
---

# Read-File Prefix Corruption Repair

## What this is

Hermes `read_file` returns content as `LINE_NUM|CONTENT` (for example `     1|# Title`). If any agent feeds that output back into `write_file`, `patch`, or skill content, the prefix becomes part of the file body. If the damaged file is then read and rewritten again, prefixes nest: `     1|     1|# Title`.

This silently degrades any text file the agent later reads as instructions or content.

## When to use

- A documentation file renders with a strange left margin in the terminal or editor.
- The first line of a file shows `     1|...` instead of the real first line.
- Several files in the same directory look similarly malformed.
- An agent recently used `read_file` followed by `write_file` or `patch` on the same path.

## Detection

One-liner over markdown files in a directory:

```bash
cd <target_dir> && find . -maxdepth 3 -name "*.md" -exec sh -c \
  'head -1 "$1" | grep -qE "^[[:space:]]*[0-9]+\|" && echo "CORRUPT: $1"' _ {} \;
```

Adjust the pattern (`*.yaml`, `*.txt`) for other formats.

To inspect nesting depth on a known-damaged file:

```bash
head -3 file.md
# CLEAN:    "# Heading"
# SINGLE:   "     1|# Heading"
# NESTED:   "     1|     1|# Heading"
```

## Repair (idempotent, with backups)

Always back up originals first. Apply the strip regex twice to handle two-level nesting; loop more times if you find depth 3+.

```bash
cd <target_dir> && for f in <file1> <file2> <file3>; do
  cp "$f" "${f}.bak-corruption-$(date +%Y%m%d)"
  python3 -c "
import re
with open('$f') as fh: t = fh.read()
fixed = re.sub(r'^\s*\d+\|', '', t, flags=re.M)
fixed = re.sub(r'^\s*\d+\|', '', fixed, flags=re.M)
with open('$f','w') as fh: fh.write(fixed)
print('fixed $f')
"
done
```

Replace the file list with whatever the detection step found.

## Verification

```bash
head -3 <repaired-file>            # should look normal
search_files target=files pattern=".bak-corruption-*"   # backups exist
```

If detection still flags the file, run another strip pass — there were more nesting levels than expected.

## Pitfalls

- Do not delete the backups in the same session. If the regex over-stripped (for example, a file legitimately starts lines with `123|`, like a pipe-table caption), you will need them.
- Pure markdown pipe tables start with `|`, not `<spaces><digits>|`, so the regex is safe. Still spot-check unusual files.
- Do not run this on `.jsonl` session transcripts — they may contain quoted `read_file` output as legitimate data.
- The root cause is upstream: the agent that saved the prefixed content. Add a guard in your write path that rejects content where most lines match `^\s*\d+\|` — it is almost certainly raw display output, not real file content.

## Why it happens

`read_file` is designed for human-readable display with line numbers. Agents that copy its output verbatim into `write_file`, or use it as the `new_string` in a `patch` call, bake the display formatting into the file. The fix above removes the symptom; the cure is teaching the agent (or a write-path guard) to never round-trip `read_file` output without stripping the prefix first.

## Related

- Hermes `read_file` output format: `LINE_NUM|CONTENT` per line.
- For agent write hygiene: prefer `patch` (whose `old_string`/`new_string` is matched against actual file content) over `write_file` when modifying existing files.
