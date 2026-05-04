# Warboard v4 Payload Schema

Every field. Nothing optional if you want a complete dashboard.

```json
{
  "version": 4,

  "commandStrip": {
    "daysToBMI": 93,
    "activeAgents": 2,
    "unreadEmails": 0,
    "openBlockers": 3,
    "patentCount": 7
  },

  "countdown": {
    "label": "Replace $20k/mo before BMI ends",
    "deadline": "2026-06-30",
    "hoursLeft": 2238
  },

  "dailyProgress": {
    "sessionsCompleted": 6,
    "deploys": 1,
    "emailsSent": 0,
    "issuesResolved": 5
  },

  "agents": [{
    "engine": "claude-code|codex|gemini|hermes|skippy",
    "task": "Short description",
    "status": "running|done|error",
    "company": "Project name (matches companies)",
    "progress": 75,
    "detail": "Markdown. What it did and WHY it matters strategically.",
    "timestamp": "2026-03-28T20:20:00Z"
  }],

  "companies": [{
    "name": "Aposema",
    "status": "green|yellow|red",
    "metric": "$0 -> $50M",
    "detail": "One-line summary",
    "details": "### Markdown\nExpanded strategic context. How this project connects to North Stars.",
    "items": [
      {"status": "✅|🟡|🔴|⏳|⚠️", "label": "Short", "value": "Description"}
    ],
    "progress": 35
  }],

  "email": {
    "accounts": [
      {"label": "tyler@aposema.com", "unread": 0, "pending_drafts": 0, "status": "clear|warn|error", "note": "optional"}
    ],
    "outbound": {"sent": 0, "failed": 0, "queued": 0, "success_rate": 100},
    "agent": {
      "running": true,
      "last_poll": "2026-03-29T11:30:00Z",
      "poll_count": 2,
      "actions": {"escalate": 0}
    },
    "summary": "Human-readable triage summary"
  },

  "pipeline": [{
    "name": "Aposema|Redline|Envelope|Klasificados",
    "current": 0,
    "target": 50000000,
    "model": "How revenue/milestone is achieved",
    "isMilestone": false
  }],

  "market": {
    "decision": "YES|CAUTION|NO",
    "score": 75,
    "mode": "swing|day",
    "pillars": {
      "volatility": {"score": 60, "vix": 18.5, "vix_trend": "falling"}
    },
    "execution_window": {"score": 80},
    "prices": {"SPY": 520, "QQQ": 440, "VIX": 18},
    "alerts": ["string"],
    "timestamp": "ISO"
  },

  "blockers": [{
    "status": "🔴|🟡|🟢",
    "project": "Name",
    "what": "One-line",
    "detail": "Markdown context. What it blocks and what to do."
  }],

  "inFlight": [{
    "project": "Name",
    "what": "One-line",
    "detail": "Markdown context."
  }],

  "lists": [{
    "title": "Tyler's Plate",
    "icon": "🎯",
    "color": "#f87171",
    "id": "unique-id",
    "items": [{
      "title": "Task name",
      "subtitle": "Context",
      "icon": "emoji",
      "body": "Optional markdown detail",
      "actions": [{
        "label": "Button text",
        "type": "resolve_blocker|snooze_blocker|check_status|approve_draft",
        "style": "green|yellow|red|accent|blue",
        "target": "identifier"
      }]
    }]
  }],

  "staleFollowups": 1,

  "bookmarks": [{
    "label": "Display name",
    "url": "https://...",
    "project": "Project name"
  }],

  "sweepNotes": "Markdown string or array of strings. What changed since last push. Tyler reads this FIRST.",

  "dailyProgress": {
    "sessionsCompleted": 6,
    "deploys": 1,
    "emailsSent": 0,
    "issuesResolved": 5
  }
}
```

## North Stars (reference for strategic context)

| Target | Metric | Deadline |
|--------|--------|----------|
| Aposema acquisition | $50M | When ready |
| BMI replacement | $20K/mo | June 30, 2026 |
| Envelope stars | 1,000 | June 30 |
| Redline MRR | $10K | June 30 |
| Klasificados | 100K listings | Maintain |
| AGI window | Ship before market closes | Dec 31, 2026 |
