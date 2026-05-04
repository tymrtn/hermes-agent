#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request, error

TOKEN = "075e3a492fc894060a59fdb47ec89744b5164d3f7da2274b"
PRIMARY_URL = "https://warboard.tmrtn.com/api/heartbeat"
FALLBACK_URL = "https://warboard-production-5cff.up.railway.app/api/heartbeat"
ACTIONS_URL = "https://warboard.tmrtn.com/api/actions/pending"

WORKSPACE = Path(__file__).resolve().parents[3]
HEARTBEAT_PATH = WORKSPACE / "HEARTBEAT.md"
TODAY_MEMORY_PATH = WORKSPACE / "memory" / "2026-04-08.md"
YESTERDAY_MEMORY_PATH = WORKSPACE / "memory" / "2026-04-07.md"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def extract_section(markdown: str, heading: str) -> list[str]:
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, markdown, re.M | re.S)
    if not match:
        return []
    lines = []
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if line.startswith("- "):
            lines.append(line[2:].strip())
    return lines


def strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.replace("—", " - ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hours_until_deadline(deadline_iso: str) -> int:
    now = datetime.now(timezone.utc)
    deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    return max(0, math.floor((deadline - now).total_seconds() / 3600))


def fetch_json(url: str, auth: bool = False) -> dict[str, Any]:
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = request.Request(url, headers=headers)
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health_status(url: str) -> tuple[str, str]:
    try:
        req = request.Request(url, headers={"User-Agent": "warboard-builder/1.0"})
        with request.urlopen(req, timeout=15) as resp:
            return ("green", f"HTTP {resp.status}")
    except Exception as exc:
        return ("red", str(exc))


def hostname_status(hostname: str) -> tuple[str, str]:
    import socket

    try:
        socket.gethostbyname(hostname)
        return ("green", "resolves")
    except Exception as exc:
        return ("red", str(exc))


def build_payload() -> dict[str, Any]:
    heartbeat = read_text(HEARTBEAT_PATH)
    today_memory = read_text(TODAY_MEMORY_PATH)
    yesterday_memory = read_text(YESTERDAY_MEMORY_PATH)
    now = iso_now()

    top_priorities = [strip_md(x) for x in extract_section(heartbeat, "Current Top Priorities")]
    in_flight_lines = [strip_md(x) for x in extract_section(heartbeat, "In-Flight")]
    tyler_plate_lines = [strip_md(x) for x in extract_section(heartbeat, "Tyler's Plate")]
    snapshot_lines = [strip_md(x) for x in extract_section(heartbeat, "Status Snapshot")]
    working_refs = [strip_md(x) for x in extract_section(heartbeat, "Working References")]
    countdown_lines = [strip_md(x) for x in extract_section(heartbeat, "Countdown")]

    warboard_health, warboard_note = health_status("https://warboard.tmrtn.com/api/health")
    redline_health, redline_note = health_status("https://redline.aposema.com")
    klasificados_health, klasificados_note = health_status("https://klasificados.net")
    governor_health, governor_note = hostname_status("governor.tmrtn.com")

    pending_actions = 0
    try:
        pending_actions = int(fetch_json(ACTIONS_URL, auth=True).get("count", 0))
    except Exception:
        pending_actions = 0

    companies = [
        {
            "name": "Aposema",
            "status": "yellow",
            "metric": "$0 -> $50M",
            "detail": "IP-sale path is still the North Star, but the active move is tightening the outbound and patent narrative.",
            "details": "### Strategic role\nAposema is the crown-jewel IP portfolio Tyler wants to sell, not nurture into a big operating company. The point of the dashboard is to keep it framed as a monetizable asset with proof points, not a distraction factory.\n\n### Current reality\n- IETF/Aynaud reply still needs recalibration before sending\n- Patent 6 waits until Tyler is home\n- Patent 4 remains the crown jewel in the filed stack",
            "items": [
                {"status": "🟡", "label": "IETF reply", "value": "Draft exists at drafts/ietf-aynaud-reply.md but is still unsent"},
                {"status": "⏳", "label": "Patent 6", "value": "Explicitly paused until Tyler gets home"},
                {"status": "✅", "label": "Portfolio framing", "value": "Sell Aposema/IP, build other businesses"},
            ],
        },
        {
            "name": "Envelope",
            "status": "yellow",
            "metric": "0 / 1,000 stars",
            "detail": "Infrastructure asset with leverage, but mail operations still show friction Tyler will notice.",
            "details": "### Strategic role\nEnvelope is leverage infrastructure: useful on its own, but more valuable because it supports outbound execution and the surrounding toolchain.\n\n### Current reality\n- IMAP archive failure on ty@tmrtn.com is still unresolved\n- editor@spainexpat.com is still missing from Envelope config\n- Tyler likely wants a notes column in the CLI",
            "items": [
                {"status": "⚠️", "label": "IMAP archive", "value": "Could not parse command moving Gmail mail to [Gmail]/All Mail"},
                {"status": "⚠️", "label": "Missing account", "value": "editor@spainexpat.com still not configured"},
                {"status": "🟡", "label": "CLI polish", "value": "Likely need notes column support"},
            ],
        },
        {
            "name": "Redline",
            "status": redline_health,
            "metric": "$0 / $10K MRR",
            "detail": f"Live and reachable ({redline_note}), but the first real customer is still the missing piece.",
            "details": "### Strategic role\nRedline is one of the leverage assets Tyler can monetize faster than the moonshot projects. It matters because it can create real cash before June 30.\n\n### Current reality\n- Site is up\n- First customer still not identified\n- Admin API works, but review-listing title coverage is still a known feature gap from earlier work",
            "items": [
                {"status": "✅", "label": "Live", "value": redline_note},
                {"status": "🟡", "label": "Revenue", "value": "First customer still needs to be identified"},
                {"status": "🟡", "label": "Product gap", "value": "Admin review listing still lacks full title coverage"},
            ],
        },
        {
            "name": "Klasificados",
            "status": "yellow",
            "metric": "100K listings",
            "detail": f"Core site is up ({klasificados_note}), but the alert-signup auth-modal regression is a fresh sharp edge.",
            "details": "### Strategic role\nKlasificados is one of the two businesses Tyler chose to build. It has to feel alive, trustworthy, and conversion-ready, not just technically online.\n\n### Current reality\n- Site is reachable\n- Visitor suite still shows one critical failure in alert signup\n- Payment setup remains incomplete",
            "items": [
                {"status": "✅", "label": "Reachability", "value": klasificados_note},
                {"status": "🔴", "label": "Alert signup", "value": "Submit fires, but the email auth modal does not appear"},
                {"status": "🟡", "label": "Payments", "value": "Still incomplete"},
            ],
        },
        {
            "name": "Loftly",
            "status": "yellow",
            "metric": "build path",
            "detail": "Still one of the two build bets, but there is no fresh execution pulse in the current heartbeat context.",
            "details": "### Strategic role\nLoftly remains one of Tyler's chosen build paths alongside Klasificados. The dashboard should keep it visible without pretending there was fresh movement today.\n\n### Current reality\n- Still strategically chosen\n- No new execution update surfaced in the current heartbeat inputs",
            "items": [
                {"status": "✅", "label": "Priority", "value": "Loftly remains one of the chosen build tracks"},
                {"status": "🟡", "label": "Fresh movement", "value": "No new visible update in current heartbeat context"},
            ],
        },
        {
            "name": "Expatriator",
            "status": "yellow",
            "metric": "leveraged niche",
            "detail": "Useful niche product, but currently more notable for email/config drag than product motion.",
            "details": "### Strategic role\nExpatriator is not the main bet, but it sits in a domain Tyler understands cold and can still turn into leverage or a sale if momentum appears.\n\n### Current reality\n- editor@spainexpat.com mailbox is still missing from Envelope config\n- No product-fire surfaced in the current heartbeat inputs",
            "items": [
                {"status": "⚠️", "label": "Mailbox gap", "value": "editor@spainexpat.com still missing from Envelope"},
                {"status": "🟡", "label": "Status", "value": "Quiet, with no fresh product movement in current context"},
            ],
        },
        {
            "name": "BMI/Musark",
            "status": "yellow",
            "metric": "$20K/mo replacement clock",
            "detail": "Still paying, but strategically deprioritized and now mostly a timer Tyler has to outrun.",
            "details": "### Strategic role\nBMI/Musark is not the destination. It is the countdown clock that forces urgency on everything else.\n\n### Current reality\n- BMI payment received Apr 3\n- Contract runway ends June 30\n- NVIDIA submission now outranks Musark work when Tyler is back",
            "items": [
                {"status": "✅", "label": "Cash", "value": "BMI payment of $4,280 landed via Wise on Apr 3"},
                {"status": "🟡", "label": "Runway", "value": "83 days left to replace the income"},
                {"status": "🟡", "label": "Priority", "value": "Strategically deprioritized relative to the chosen projects"},
            ],
        },
        {
            "name": "Governor",
            "status": governor_health,
            "metric": "reachability",
            "detail": f"Still DNS-dead ({governor_note}), already escalated once, and now suppressed until the status changes.",
            "details": "### Strategic role\nGovernor matters as infrastructure and leverage, but right now the operational rule is simple: do not let this consume heartbeat attention with repetitive noise.\n\n### Current reality\n- NXDOMAIN / unresolved host behavior persists\n- Escalated once after 6+ unchanged failures\n- Suppress routine repeats until status changes",
            "items": [
                {"status": "🔴", "label": "DNS", "value": governor_note},
                {"status": "✅", "label": "Escalation policy", "value": "Already escalated once, now suppressed until change"},
            ],
        },
        {
            "name": "Warboard",
            "status": warboard_health,
            "metric": "operating spine",
            "detail": f"Dashboard is reachable ({warboard_note}) and this refresh restores it to a full v4 payload instead of the half-empty sludge Tyler was seeing.",
            "details": "### Strategic role\nWarboard is the operating spine Tyler should be able to trust from his phone. If it feels stale, vague, or half-rendered, it fails its job.\n\n### Current reality\n- API health is green\n- Full payload is being republished from current HEARTBEAT and memory context\n- The goal of this refresh is to reduce likely complaints: better sweep notes, clearer blockers, richer lists, and a coherent portfolio picture",
            "items": [
                {"status": "✅", "label": "Reachability", "value": warboard_note},
                {"status": "✅", "label": "Payload", "value": "Full v4 payload rebuilt from current state"},
                {"status": "🟡", "label": "Next polish", "value": "Frontend-specific enhancements can happen separately if Tyler still wants more"},
            ],
        },
    ]

    bookmarks = [
        {"label": "Warboard", "url": "https://warboard.tmrtn.com", "project": "Warboard"},
        {"label": "Redline", "url": "https://redline.aposema.com", "project": "Redline"},
        {"label": "Klasificados", "url": "https://klasificados.net", "project": "Klasificados"},
        {"label": "SSRN paper", "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6432898", "project": "Aposema"},
        {"label": "IETF reply draft", "url": "file://" + str((WORKSPACE / "drafts" / "ietf-aynaud-reply.md").resolve()), "project": "Aposema"},
    ]

    blockers = [
        {
            "status": "🔴",
            "project": "Nagatha/Hermes",
            "what": "Sandbox tightening is still urgent",
            "detail": "Wrapper process sprawl, hanging Codex audit, and rogue `rg` scans are still traversing `/Users/tylermartin`. This is both noise and risk. The next real move is root-cause cleanup, not more observing.",
        },
        {
            "status": "🔴",
            "project": "Klasificados",
            "what": "Alert-signup auth modal regression is still untriaged",
            "detail": "Fresh visitor-suite results showed one critical failure: alert submit fired, but the email auth modal never appeared. That blocks a core conversion path on a chosen build project.",
        },
        {
            "status": "🟡",
            "project": "Envelope",
            "what": "Inbox automation is degraded",
            "detail": "`ty@tmrtn.com` still hits the Gmail archive parse failure, and `editor@spainexpat.com` is still missing from Envelope. This is operational drag Tyler will absolutely notice.",
        },
        {
            "status": "🟡",
            "project": "Aposema",
            "what": "IETF/Aynaud reply is still parked",
            "detail": "The reply draft exists, but it still needs a re-read and recalibration before sending. Leaving it here is fine for one day; leaving it here forever is monkey behavior.",
        },
    ]

    inflight = [
        {
            "project": "NVIDIA",
            "what": "Product profile + submission becomes first priority when Tyler is back",
            "detail": "There is already a review follow-up due Apr 9, and HEARTBEAT makes it explicit that the product profile and submission jump to the top of the queue once Tyler is back in Madrid.",
        },
        {
            "project": "Travel",
            "what": "Egypt trip is still active",
            "detail": "Tyler is still traveling, which means operations should stay crisp and minimal: fewer fake tasks, more sharp blockers, less dashboard sludge.",
        },
        {
            "project": "Aposema",
            "what": "Patent 6 waits until Tyler gets home",
            "detail": "This is intentionally paused. The dashboard should show it as parked, not as a mysterious omission.",
        },
        {
            "project": "Redline",
            "what": "First customer still needs to be identified",
            "detail": "This is not a technical blocker, it is a go-to-market gap. Keeping it visible matters because revenue before June 30 matters more than neat software architecture.",
        },
    ]

    lists = [
        {
            "title": "Tyler's Plate",
            "icon": "🎯",
            "color": "#f87171",
            "id": "tyler-plate",
            "items": [
                {
                    "title": item.split(" — ", 1)[0],
                    "subtitle": item.split(" — ", 1)[1] if " — " in item else "Current priority from HEARTBEAT.md",
                    "icon": "•",
                    "body": f"Source: HEARTBEAT.md\\n\\n{item}",
                    "actions": [{"label": "Check status", "type": "check_status", "style": "blue", "target": item}],
                }
                for item in tyler_plate_lines[:7]
            ],
        },
        {
            "title": "Recent Wins",
            "icon": "✅",
            "color": "#34d399",
            "id": "recent-wins",
            "items": [
                {
                    "title": "Warboard, Redline, and Klasificados all verified live",
                    "subtitle": "Fresh checks still returned 200s",
                    "icon": "✅",
                    "body": "These are the boring-but-important green lights: Warboard, Redline, and Klasificados are all reachable right now.",
                },
                {
                    "title": "SSRN paper approved",
                    "subtitle": "ID 6432898",
                    "icon": "📄",
                    "body": "The paper is approved and live. That is real signal, not dashboard confetti.",
                },
                {
                    "title": "BMI payment landed",
                    "subtitle": "$4,280 via Wise on Apr 3",
                    "icon": "💸",
                    "body": "Useful because runway pressure is still real, even if BMI is not the strategic destination.",
                },
            ],
        },
        {
            "title": "Inbox Fires",
            "icon": "📧",
            "color": "#60a5fa",
            "id": "inbox-fires",
            "items": [
                {
                    "title": "Urgent unread items surfaced",
                    "subtitle": "4 urgent unread items in the latest inbox monitor",
                    "icon": "⚠️",
                    "body": "Latest heartbeat noted Digital Trends bounces, Wise transfer sent, Archive.org reply, and bunny.net invoice in the urgent unread set.",
                    "actions": [{"label": "Check status", "type": "check_status", "style": "blue", "target": "urgent-inbox-items"}],
                },
                {
                    "title": "Gmail archive failure",
                    "subtitle": "IMAP could not parse move to [Gmail]/All Mail",
                    "icon": "🟡",
                    "body": "This is the operational paper-cut in Envelope that should stay visible until fixed.",
                    "actions": [{"label": "Check status", "type": "check_status", "style": "yellow", "target": "gmail-archive-failure"}],
                },
                {
                    "title": "Missing SpainExpat editor account",
                    "subtitle": "editor@spainexpat.com still not configured",
                    "icon": "🔴",
                    "body": "Not glamorous, but exactly the kind of omission Tyler will rightly complain about if it keeps hanging around.",
                    "actions": [{"label": "Check status", "type": "check_status", "style": "red", "target": "editor-spainexpat-config"}],
                },
            ],
        },
        {
            "title": "Working Drafts & Files",
            "icon": "🗂️",
            "color": "#a78bfa",
            "id": "working-files",
            "items": [
                {
                    "title": "IETF/Aynaud reply draft",
                    "subtitle": str(WORKSPACE / "drafts" / "ietf-aynaud-reply.md"),
                    "icon": "📄",
                    "body": "This draft exists and needs recalibration before sending.",
                },
                {
                    "title": "HEARTBEAT source of truth",
                    "subtitle": str(HEARTBEAT_PATH),
                    "icon": "🫀",
                    "body": "This warboard refresh is derived primarily from the current HEARTBEAT.md state.",
                },
                {
                    "title": "Warboard action poller",
                    "subtitle": str(WORKSPACE / "email-copilot" / "warboard-actions.py"),
                    "icon": "🤖",
                    "body": "Existing helper that polls Tyler's pending Warboard actions and marks them complete.",
                },
                {
                    "title": "Full-payload builder",
                    "subtitle": str(Path(__file__).resolve()),
                    "icon": "🛠️",
                    "body": "New explicit builder/push path so Warboard stops rotting back to a partial payload.",
                },
            ],
        },
        {
            "title": "Mini Redline Dashboard",
            "icon": "📝",
            "color": "#f87171",
            "id": "mini-redline",
            "items": [
                {
                    "title": "Availability",
                    "subtitle": redline_note,
                    "icon": "✅" if redline_health == "green" else "🔴",
                    "body": "Redline is reachable right now. Good. Now it needs money, not admiration.",
                },
                {
                    "title": "Revenue gap",
                    "subtitle": "First customer still unidentified",
                    "icon": "🟡",
                    "body": "The product is live, but the dashboard should keep the revenue gap visible until there is an actual paying user.",
                    "actions": [{"label": "Check status", "type": "check_status", "style": "yellow", "target": "redline-first-customer"}],
                },
                {
                    "title": "Product gap",
                    "subtitle": "Admin review listing still lacks some title coverage",
                    "icon": "🟡",
                    "body": "Not fatal, but worth tracking because it affects operational clarity for review management.",
                },
            ],
        },
        {
            "title": "Recent Links",
            "icon": "🔗",
            "color": "#94a3b8",
            "id": "recent-links",
            "items": [
                {
                    "title": "Warboard live app",
                    "subtitle": "https://warboard.tmrtn.com",
                    "icon": "🧭",
                    "body": "Primary dashboard URL.",
                },
                {
                    "title": "Redline",
                    "subtitle": "https://redline.aposema.com",
                    "icon": "📝",
                    "body": "Current live Redline instance.",
                },
                {
                    "title": "Klasificados",
                    "subtitle": "https://klasificados.net",
                    "icon": "📦",
                    "body": "Current live Klasificados instance.",
                },
                {
                    "title": "SSRN paper",
                    "subtitle": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6432898",
                    "icon": "📚",
                    "body": "Approved paper reference Tyler may want from his phone.",
                },
            ],
        },
    ]

    payload = {
        "version": 4,
        "commandStrip": {
            "daysToBMI": 83,
            "activeAgents": 0,
            "unreadEmails": 4,
            "openBlockers": len(blockers),
            "patentCount": 7,
        },
        "countdown": {
            "label": "Replace $20k/mo before BMI ends",
            "deadline": "2026-06-30T21:59:59Z",
            "hoursLeft": hours_until_deadline("2026-06-30T21:59:59Z"),
        },
        "dailyProgress": {
            "sessionsCompleted": 4,
            "deploys": 0,
            "emailsSent": 0,
            "issuesResolved": 2,
        },
        "agents": [],
        "companies": companies,
        "projects": companies,
        "email": {
            "accounts": [
                {"label": "ty@tmrtn.com", "unread": 4, "pending_drafts": 0, "status": "warn", "note": "Gmail archive parse error still open"},
                {"label": "editor@spainexpat.com", "unread": 0, "pending_drafts": 0, "status": "error", "note": "Mailbox still not configured in Envelope"},
                {"label": "tyler@aposema.com", "unread": 0, "pending_drafts": 0, "status": "warn", "note": "Earlier inbox/folder HTTP errors need recheck"},
                {"label": "skippy@aposema.com", "unread": 0, "pending_drafts": 0, "status": "warn", "note": "Earlier folder HTTP errors need recheck"},
            ],
            "outbound": {"sent": 0, "failed": 0, "queued": 0, "success_rate": 100},
            "agent": {"running": True, "last_poll": now, "poll_count": 1, "actions": {"escalate": pending_actions}},
            "summary": "Inbox monitor surfaced 4 urgent unread items. The sharper problem is operational: Gmail archive failures persist and editor@spainexpat.com is still missing from Envelope.",
        },
        "pipeline": [
            {"name": "Aposema", "current": 0, "target": 50000000, "model": "Sell the IP portfolio, not the operating company", "isMilestone": False},
            {"name": "Redline", "current": 0, "target": 10000, "model": "$10 per review into $10K MRR", "isMilestone": False},
            {"name": "Klasificados", "current": 100000, "target": 100000, "model": "Maintain 100K listings while fixing conversion paths", "isMilestone": True},
            {"name": "Envelope", "current": 0, "target": 1000, "model": "OSS leverage asset, measured here as GitHub stars", "isMilestone": True},
        ],
        "market": {
            "decision": "CAUTION",
            "score": 35,
            "mode": "swing",
            "pillars": {
                "volatility": {"score": 50, "vix": 0, "vix_trend": "falling"},
                "trend": {"score": 30},
                "momentum": {"score": 30},
                "breadth": {"score": 35},
                "macro": {"score": 30},
            },
            "execution_window": {"score": 30},
            "prices": {"SPY": "stale", "QQQ": "stale", "VIX": "stale"},
            "alerts": ["No fresh market sweep in the current heartbeat context. Treat this section as intentionally cautious, not as a trading signal."],
            "timestamp": now,
        },
        "blockers": blockers,
        "inFlight": inflight,
        "lists": lists,
        "staleFollowups": 0,
        "bookmarks": bookmarks,
        "sweepNotes": [
            "Full v4 payload restored so Warboard is no longer mostly null.",
            "Re-centered the board on what Tyler will actually care about: Hermes cleanup, Klasificados conversion bug, inbox friction, NVIDIA next, and revenue pressure before June 30.",
            "Added richer phone-friendly lists for Tyler's plate, inbox fires, working files, recent links, and a mini Redline dashboard.",
            f"Live checks still show Warboard ({warboard_note}), Redline ({redline_note}), and Klasificados ({klasificados_note}) up, while Governor remains suppressed and DNS-dead ({governor_note}).",
        ],
    }

    payload["_sources"] = {
        "heartbeat_last_updated": re.search(r"Last updated: (.+)", heartbeat).group(1) if "Last updated:" in heartbeat else None,
        "countdown_lines": countdown_lines,
        "top_priorities": top_priorities,
        "status_snapshot": snapshot_lines,
        "in_flight": in_flight_lines,
        "today_memory_excerpt_present": bool(today_memory.strip()),
        "yesterday_memory_excerpt_present": bool(yesterday_memory.strip()),
    }
    return payload


def verify_payload(data: dict[str, Any]) -> None:
    required = [
        "version", "commandStrip", "countdown", "dailyProgress", "agents", "companies",
        "email", "pipeline", "market", "blockers", "inFlight", "lists", "staleFollowups",
        "bookmarks", "sweepNotes",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise SystemExit(f"Missing required payload keys: {', '.join(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and optionally push a full Warboard payload.")
    parser.add_argument("--output", help="Write payload JSON to this path.")
    parser.add_argument("--push", action="store_true", help="POST the payload to the Warboard API.")
    parser.add_argument("--verify", action="store_true", help="Fetch the live heartbeat after push and verify key sections are present.")
    parser.add_argument("--stability-seconds", type=int, default=0, help="After push, wait this many seconds and confirm the full payload was not overwritten.")
    args = parser.parse_args()

    payload = build_payload()
    verify_payload(payload)

    output_payload = dict(payload)
    output_payload.pop("_sources", None)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output_payload, indent=2) + "\n", encoding="utf-8")

    if args.push:
        result = None
        errors: list[str] = []
        for url in (PRIMARY_URL, FALLBACK_URL):
            try:
                result = post_json(url, output_payload)
                print(f"PUSH_OK {url} {json.dumps(result)}")
                break
            except error.HTTPError as exc:
                errors.append(f"{url} HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')}")
            except Exception as exc:
                errors.append(f"{url} ERROR: {exc}")
        if result is None:
            raise SystemExit("Push failed:\n" + "\n".join(errors))

    if args.verify:
        live = fetch_json(PRIMARY_URL, auth=False)
        verify_payload(live)
        verify_summary = {
            "verify": "ok",
            "version": live.get("version"),
            "companies": len(live.get("companies") or []),
            "lists": len(live.get("lists") or []),
            "blockers": len(live.get("blockers") or []),
            "bookmarks": len(live.get("bookmarks") or []),
            "has_email": live.get("email") is not None,
            "has_market": live.get("market") is not None,
            "has_pipeline": live.get("pipeline") is not None,
            "receivedAt": live.get("receivedAt"),
        }
        print(json.dumps(verify_summary, indent=2))

        if args.stability_seconds > 0:
            time.sleep(args.stability_seconds)
            stable = fetch_json(PRIMARY_URL, auth=False)
            stability = {
                "stability_check": args.stability_seconds,
                "receivedAt": stable.get("receivedAt"),
                "commandStrip_type": type(stable.get("commandStrip")).__name__,
                "email_type": type(stable.get("email")).__name__,
                "market_type": type(stable.get("market")).__name__,
                "dailyProgress_type": type(stable.get("dailyProgress")).__name__,
                "pipeline_type": type(stable.get("pipeline")).__name__,
                "lists_count": len(stable.get("lists") or []),
                "companies_count": len(stable.get("companies") or []),
                "bookmarks_count": len(stable.get("bookmarks") or []),
                "overwritten": any(
                    [
                        stable.get("commandStrip") is None,
                        stable.get("email") is None,
                        stable.get("market") is None,
                        stable.get("dailyProgress") is None,
                        stable.get("pipeline") is None,
                        not (stable.get("lists") or []),
                    ]
                ),
            }
            print(json.dumps(stability, indent=2))
    elif not args.output and not args.push:
        print(json.dumps(output_payload, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
