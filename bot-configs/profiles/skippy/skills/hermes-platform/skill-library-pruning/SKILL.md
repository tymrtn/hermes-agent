---
name: skill-library-pruning
description: Audit and prune Skippy's active skill library by finding oversized, duplicate, stale, or out-of-scope skills; archive outside active discovery; verify counts before/after.
tags: [skills, hermes, pruning, archive, maintenance, bloat]
triggers: ["skill bloat", "too many skills", "archive skills", "prune skills", "consolidate skills", "why do we have these skills"]
---

# Skill Library Pruning

Use when Tyler asks about skill bloat, duplicate skills, out-of-scope skills, or wants active skills archived/cleaned up.

## Core policy

Active skills should reflect Tyler's current operating stack. Generic capability packs and stale imports should be moved out of active discovery, not deleted.

Current Tyler-specific priority stack:
- Governor / Aposema / IP
- Klasificados / Loftly
- SpainExpat / WordPress migration
- Envelope / Redline / Clef
- GitHub/dev workflow
- Todoist human-action layer
- email/outreach
- Warboard/Hermes autonomy

ML/data-science skills are generally cold storage unless Tyler explicitly resumes ML work. Do not keep large ML reference skills active by default.

## Audit commands

Count active skills and identify size offenders:

```bash
python3 - <<'PY'
from pathlib import Path
from collections import defaultdict
root=Path('/Users/wondermonkey/.hermes/profiles/skippy/skills')
files=list(root.rglob('SKILL.md'))
cat=defaultdict(list)
for p in files:
    c=p.relative_to(root).parts[0]
    txt=p.read_text(errors='ignore')
    cat[c].append((str(p.relative_to(root)), p.stat().st_size, txt.count('\n')+1))
print('TOTAL_SKILLS', len(files))
print('TOTAL_BYTES', sum(p.stat().st_size for p in files))
print('TOTAL_LINES', sum(p.read_text(errors='ignore').count('\n')+1 for p in files))
print('\nBY_CATEGORY')
for c,items in sorted(cat.items(), key=lambda kv:(-len(kv[1]), kv[0])):
    print(f'{c}\t{len(items)}\t{sum(i[1] for i in items)} bytes\t{sum(i[2] for i in items)} lines')
print('\nLARGEST')
for rel,size,lines in sorted([(str(p.relative_to(root)), p.stat().st_size, p.read_text(errors='ignore').count('\n')+1) for p in files], key=lambda x:-x[1])[:20]:
    print(f'{size:7} bytes {lines:5} lines {rel}')
PY
```

Find likely duplicate/similar skills with a lightweight TF-IDF pass over names, descriptions, and headings:

```bash
python3 - <<'PY'
from pathlib import Path
import re, math
from collections import Counter
root=Path('/Users/wondermonkey/.hermes/profiles/skippy/skills')
skills=[]
for p in root.rglob('SKILL.md'):
    txt=p.read_text(errors='ignore')
    rel=str(p.relative_to(root)).replace('/SKILL.md','')
    name=re.search(r'^name:\s*([^\n]+)', txt, re.M)
    desc=re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', txt, re.M)
    skills.append({'rel':rel,'text':txt,'name':name.group(1).strip() if name else rel,'desc':desc.group(1).strip() if desc else ''})
stop=set('the a an and or for to of in on with use when by from this that skill skills tyler skippy hermes cli api via into as is are be do not if'.split())
def toks(s): return [t for t in re.findall(r'[a-z0-9][a-z0-9_-]{2,}', s.lower()) if t not in stop]
for sk in skills:
    headings=' '.join(re.findall(r'^#+\s+(.+)$', sk['text'], re.M))
    sk['tok']=Counter(toks(sk['rel']+' '+sk['name']+' '+sk['desc']+' '+headings))
N=len(skills); df=Counter()
for sk in skills: df.update(sk['tok'].keys())
idf={t:math.log((N+1)/(c+1))+1 for t,c in df.items()}
for sk in skills:
    sk['vec']={t:v*idf[t] for t,v in sk['tok'].items()}
    sk['norm']=math.sqrt(sum(v*v for v in sk['vec'].values())) or 1
pairs=[]
for i,a in enumerate(skills):
    for b in skills[i+1:]:
        common=set(a['vec'])&set(b['vec'])
        sim=sum(a['vec'][t]*b['vec'][t] for t in common)/(a['norm']*b['norm'])
        if sim>0.22: pairs.append((sim,a['rel'],b['rel']))
for sim,a,b in sorted(pairs, reverse=True)[:60]:
    print(f'{sim:.3f}\t{a}\t{b}')
PY
```

## Archive, do not delete

Move stale/out-of-scope skills outside the active `skills/` tree. Use a timestamped archive directory:

```bash
BASE=/Users/wondermonkey/.hermes/profiles/skippy
STAMP=$(date +%Y%m%d-%H%M%S)
ARCH="$BASE/skills-archive/$STAMP/<reason>"
mkdir -p "$ARCH"
# Example: archive entire mlops category
mv "$BASE/skills/mlops" "$ARCH/mlops"
```

For individual skills, preserve category paths:

```bash
BASE=/Users/wondermonkey/.hermes/profiles/skippy
STAMP=$(date +%Y%m%d-%H%M%S)
ARCH="$BASE/skills-archive/$STAMP/misc"
for src in "$BASE/skills/media/heartmula" "$BASE/skills/data-science/jupyter-live-kernel"; do
  if [ -d "$src" ]; then
    rel=${src#$BASE/skills/}
    mkdir -p "$ARCH/$(dirname "$rel")"
    mv "$src" "$ARCH/$rel"
  fi
done
```

## Verify after pruning

Always report before/after active skill counts, bytes, and lines:

```bash
python3 - <<'PY'
from pathlib import Path
base=Path('/Users/wondermonkey/.hermes/profiles/skippy')
active=list((base/'skills').rglob('SKILL.md'))
archives=list((base/'skills-archive').rglob('SKILL.md'))
print('active_skills', len(active))
print('active_bytes', sum(p.stat().st_size for p in active))
print('active_lines', sum(p.read_text(errors='ignore').count('\n')+1 for p in active))
print('total_archived_skill_files', len(archives))
PY
```

## Proven cleanup from 2026-04-30

Archived active `mlops` out of discovery:
- active skills: 126 → 104
- active text: ~1.28 MB → ~886 KB
- active lines: 31,282 → 22,540
- archived 22 ML skills under `skills-archive/20260430-232645/mlops`

Then archived:
- `media/heartmula`
- `data-science/jupyter-live-kernel`

Final verified state after that pass:
- active skills: 102
- active text: ~874 KB
- active lines: 22,197
- total archived skill files: 24

## Cleanup targets that remain likely useful

Potential consolidation groups:
- Todoist cluster: `todoist-delta-verification`, `todoist-live-triage`, `todoist-raw-comments`, `todoist-rest-gotchas`
- MCP cluster: `mcp/mcporter`, `mcp/native-mcp`
- OpenClaw legacy/autonomy: `openclaw-imports/dream-cycle`, `hermes-platform/hermes-dream-cycle`, `openclaw-imports/heartbeat`, `openclaw-imports/followup`, `openclaw-imports/warboard`
- Maps/location: `leisure/find-nearby`, `productivity/maps`
- GitHub/dev workflow: issues + PR workflow may be consolidated, while auth and code review likely remain separate

## Pitfalls

- Do not delete skills outright unless Tyler explicitly asks. Archive first.
- Renaming a category under `skills/` may not remove it from discovery if the loader scans recursively. Move cold skills outside `skills/`.
- Verify after every move; do not trust assumptions about loader behavior.
- Some terms like `llm`, `model`, or `github` produce false positives in broad searches. Inspect skill content before archiving.
