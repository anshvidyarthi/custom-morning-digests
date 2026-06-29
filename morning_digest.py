#!/usr/bin/env python3
"""Cool Topic Readings — daily news digest.

Run by GitHub Actions cron (or manually via `python morning_digest.py`).

Reads ledger + recent digests, calls the Anthropic API with the web_search
server tool to research the day's news, writes today's Markdown digest,
appends extracted URLs to the ledger, and emails the digest via Resend.
"""

from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

# ---- Config ----

REPO_ROOT = Path(__file__).parent.resolve()
DIGESTS_DIR = REPO_ROOT / "digests"
LEDGER = REPO_ROOT / ".covered-urls.tsv"
PROMPT_FILE = REPO_ROOT / "_prompt.md"

LOOKBACK_DAYS_LEDGER = 14
# LOOKBACK_DAYS_LEDGER now drives both URL exclusion AND the headline block,
# replacing the older "inline last N digests" approach (which buried headlines
# in prose and made semantic comparison unreliable).

MODEL = os.environ.get("DIGEST_MODEL", "claude-sonnet-4-5")
MAX_TOKENS = 16000
# Each web search call returns ~5-8K tokens of result content. With Sonnet 4.5's
# 200K context window, 30 searches would push us over the limit on busy news
# days (this happened 2026-05-14 → BadRequestError, prompt was 203K tokens).
# 15 leaves ~50K tokens of headroom while still allowing thorough research.
WEB_SEARCH_MAX_USES = 15

DIGEST_TO = os.environ.get("DIGEST_TO", "anshvidyarthi@gmail.com")
DIGEST_FROM = "Cool Topic Readings <onboarding@resend.dev>"

# ET timezone for "today" — cron runs at 13:00 UTC = 9am EDT.
ET_OFFSET_HOURS = -4  # EDT; will be -5 in EST (Nov-Mar). Acceptable drift.


# One niche per weekday. weekday() returns 0=Mon..6=Sun; weekends shouldn't fire
# (cron filters them out), but the dict covers all 7 just in case.
NICHE_KEY_BY_WEEKDAY: dict[int, str] = {
    0: "ai",                # Monday
    1: "ai_productivity",   # Tuesday
    2: "biotech",           # Wednesday
    3: "finance",           # Thursday
    4: "quantum",           # Friday
    5: "ai",                # Saturday (only if someone manually fires)
    6: "ai",                # Sunday
}

NICHE_META: dict[str, dict[str, str]] = {
    "ai": {
        "name": "AI",
        "emoji": "🤖",
        "definition": """AI — research, models, lab announcements, policy:
- Anthropic, OpenAI, DeepMind, Google AI, Meta AI, xAI blogs and changelogs
- arXiv cs.LG, cs.AI, cs.CL — most-discussed recent papers
- Hacker News (AI stories with significant discussion)
- Simon Willison's blog, Nathan Lambert's Interconnects, Ethan Mollick's One Useful Thing
- Major model releases, benchmark results (HLE, SWE-bench, AIME, MMLU-Pro, ARC)
- AI policy / regulatory news (EU AI Act, US executive orders, state-level laws)
- Frontier-lab safety publications and red-team writeups
- Notable lab papers on reasoning, alignment, interpretability""",
    },
    "ai_productivity": {
        "name": "AI Productivity",
        "emoji": "⚡",
        "definition": """AI Productivity — tools, agents, workflows, "how I use AI" essays:
- AI-powered coding tools: Cursor, Claude Code, GitHub Copilot, Windsurf, Continue, Aider, Zed AI, Replit, Vercel v0
- Agent frameworks: Anthropic Agent SDK, OpenAI Agents SDK, LangGraph, CrewAI, AutoGen, Mastra
- Browser/computer-use agents: Anthropic Computer Use, Manus, MultiOn, BrowserBase, Playwright MCP
- MCP server releases, IDE extensions, prompt libraries, skill marketplaces
- Workflow techniques from credible practitioners (Simon Willison, Geoffrey Huntley, Mitchell Hashimoto, Birchtree)
- Productivity-focused launches in adjacent tools: Notion AI, Linear, Granola, Raycast AI, Arc Browser AI, ChatGPT Atlas
- Notable agent harness benchmarks / evals comparing tools
- Skip pure model releases unless they ship a meaningful new productivity feature""",
    },
    "biotech": {
        "name": "Biotech / Longevity / Neurotech",
        "emoji": "🧬",
        "definition": """Biotech / Longevity / Neurotech:
- bioRxiv (neuroscience, aging, regenerative medicine, gene therapy preprints)
- Nature, Science news sections
- Endpoints News, Fierce Biotech, STAT
- Longevity: Lifespan.io, Rejuvenation Now, Buck Institute, NUS Healthy Longevity, Open Longevity
- Neurotech: Neuralink, Synchron, Precision Neuroscience, Paradromics, BCI/brain-computer-interface news
- FDA approvals, EMA approvals, major clinical trial readouts (Phase 2/3)
- Notable acquisitions or licensing deals in therapeutics
- Gene editing (CRISPR, prime editing), cell therapy, mRNA platform news""",
    },
    "finance": {
        "name": "Finance & Stock Markets",
        "emoji": "📈",
        "definition": """Finance & Stock Markets:
- Major US market moves: S&P 500, NASDAQ, Dow — notable single-day swings, sector rotation
- Single-stock movers >5%: meaningful catalyst (earnings beat/miss, guidance, M&A, regulatory)
- Earnings reports — beats, misses, guidance changes from notable names
- Fed: FOMC decisions, dot plot, speeches, balance-sheet actions
- Macro data: CPI, PPI, NFP, JOLTS, GDP, ISM PMI — surprises vs. consensus
- Bond market: 2y/10y/30y yields, yield-curve shape, credit spreads
- Sector trends: tech (semiconductors, AI capex), energy (oil, nat gas), financials, biotech
- IPOs, secondary offerings, notable M&A announcements
- Crypto only if material: BTC/ETH >10% moves, ETF flows, regulatory shifts
- Sources: Bloomberg, WSJ, FT, Reuters, CNBC, MarketWatch, Yahoo Finance
- Substacks: Matt Levine (Money Stuff), Joseph Politano (Apricitas Economics), Stratechery""",
    },
    "quantum": {
        "name": "Quantum",
        "emoji": "⚛️",
        "definition": """Quantum (computing, hardware, algorithms, ecosystem):
- Industry players: IBM Quantum, Google Quantum AI, Microsoft Azure Quantum, IonQ, Rigetti, PsiQuantum, Atom Computing, QuEra, Pasqal, D-Wave, Quantinuum
- arXiv quant-ph — most-discussed recent papers
- Hardware milestones: qubit count, two-qubit-gate fidelity, T1/T2 coherence times, error rates
- Quantum error correction breakthroughs (surface code, color codes, magic-state distillation)
- Quantum algorithms: VQE, QAOA, Shor's, HHL, quantum machine learning advances
- Software stacks: Qiskit, Cirq, PennyLane, Q#, Braket
- Government / institutional programs: DARPA, NSF, DOE, EU Quantum Flagship, UK National Quantum Strategy
- Investment & M&A in quantum
- Notable hires, lab announcements, conference talks (Q2B, IEEE Quantum Week)
- Sources: Quanta Magazine, IEEE Spectrum quantum coverage, Nature Physics, Physics World""",
    },
}


# ---- Helpers ----

def today_str() -> str:
    """Today's date in America/New_York (approximate via fixed offset)."""
    return datetime.now(timezone(timedelta(hours=ET_OFFSET_HOURS))).strftime("%Y-%m-%d")


def niche_for_date(date_str: str) -> dict[str, str]:
    """Return the niche metadata dict for the given YYYY-MM-DD."""
    weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    key = NICHE_KEY_BY_WEEKDAY[weekday]
    return NICHE_META[key]


def build_covered_urls(today: str) -> str:
    """Return last-14-days TSV from the ledger."""
    if not LEDGER.exists():
        return "(no entries yet — first run)"
    cutoff = (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS_LEDGER)
    ).strftime("%Y-%m-%d")
    keep = [
        line for line in LEDGER.read_text().splitlines()
        if line and line.split("\t", 1)[0] >= cutoff
    ]
    return "\n".join(keep) if keep else "(no recent entries)"


def build_recent_headlines(today: str) -> str:
    """Return last-14-days headlines grouped by date, deduped within each date.

    Same story often appears at multiple URLs in a single digest (the agent cites
    multiple sources). Collapsing to unique headlines per date gives the model
    a clean signal for semantic matching — far better than wading through prose.
    """
    if not LEDGER.exists():
        return "(no entries yet — first run)"
    cutoff = (
        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS_LEDGER)
    ).strftime("%Y-%m-%d")

    by_date: dict[str, list[str]] = {}
    seen_per_date: dict[str, set[str]] = {}

    for line in LEDGER.read_text().splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        date, _url, headline = parts[0], parts[1], parts[2]
        if date < cutoff or date == today:
            continue
        if date not in by_date:
            by_date[date] = []
            seen_per_date[date] = set()
        if headline not in seen_per_date[date]:
            by_date[date].append(headline)
            seen_per_date[date].add(headline)

    if not by_date:
        return "(no recent entries)"

    out: list[str] = []
    for date in sorted(by_date.keys(), reverse=True):
        out.append(f"{date}:")
        for h in by_date[date]:
            out.append(f"  - {h}")
        out.append("")
    return "\n".join(out).rstrip()


def render_prompt(today: str) -> str:
    template = PROMPT_FILE.read_text()
    niche = niche_for_date(today)
    return (
        template
        .replace("{TODAY}", today)
        .replace("{NICHE_NAME}", niche["name"])
        .replace("{NICHE_EMOJI}", niche["emoji"])
        .replace("{NICHE_DEFINITION}", niche["definition"])
        .replace("{COVERED_URLS}", build_covered_urls(today))
        .replace("{RECENT_HEADLINES}", build_recent_headlines(today))
    )


# ---- Agent loop ----

def run_agent(prompt: str) -> str:
    """Call the Anthropic API with the web_search server tool. Return final text."""
    client = anthropic.Anthropic()

    print(f"Calling {MODEL} with prompt of {len(prompt):,} chars...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_SEARCH_MAX_USES,
            },
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    # Server-side web_search means Anthropic runs the loop internally; the
    # response.content already contains the final assistant message text.
    text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    raw = "\n".join(text_blocks).strip()

    # The model often produces commentary or false starts before the real digest.
    # Take the substring starting at the LAST "# Cool Topic Readings" header.
    marker = "# Cool Topic Readings"
    last_idx = raw.rfind(marker)
    digest = raw[last_idx:].strip() if last_idx >= 0 else raw

    # Always save the raw response too — useful for debugging when validation fails.
    debug_path = REPO_ROOT / ".last-raw-response.md"
    debug_path.write_text(raw)
    print(f"Raw response saved to {debug_path.name} ({len(raw):,} chars)")

    # Log usage for debugging.
    if hasattr(response, "usage"):
        u = response.usage
        print(
            f"Usage: input={getattr(u, 'input_tokens', '?')} "
            f"output={getattr(u, 'output_tokens', '?')} "
            f"stop_reason={response.stop_reason}"
        )

    return digest


def validate_digest(text: str) -> None:
    """Sanity-check the digest before persisting/sending."""
    if not text.strip():
        raise RuntimeError("Empty digest from agent")
    must_contain = ["# Cool Topic Readings", "## TL;DR"]
    missing = [m for m in must_contain if m not in text]
    if missing:
        raise RuntimeError(f"Digest missing expected sections: {missing}")
    if len(text) < 1500:
        raise RuntimeError(f"Digest suspiciously short: {len(text)} chars")
    # Sanity: at least 6 items with a URL across the whole digest.
    # Allow headline and URL to be separated by up to ~600 chars (multi-line items).
    # Threshold is 6 (not 4) because we're single-niche-per-day now — going deeper
    # in one topic means 8-12 items is the target.
    bold_url_pattern = re.compile(
        r"\*\*[^*]{1,200}\*\*.{0,600}?\[[^\]]+\]\(https?://", re.DOTALL
    )
    matches = bold_url_pattern.findall(text)
    if len(matches) < 6:
        raise RuntimeError(
            f"Digest has only {len(matches)} items with URLs (need >=6) — agent "
            "likely returned a 'no news' placeholder. Raw response saved to "
            ".last-raw-response.md for debugging."
        )


# ---- Persistence ----

def write_digest(content: str, today: str) -> Path:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGESTS_DIR / f"{today}.md"
    path.write_text(content if content.endswith("\n") else content + "\n")
    return path


def extract_items(text: str) -> list[tuple[str, str]]:
    """Extract (headline, url) pairs from digest markdown."""
    items: list[tuple[str, str]] = []
    for para in text.split("\n\n"):
        m = re.match(r"\*\*([^*]+)\*\*", para)
        if not m:
            continue
        headline = m.group(1).strip().replace("\t", " ").replace("\n", " ")
        for url in re.findall(r"\((https?://[^)\s]+)\)", para):
            items.append((headline, url.replace("\t", " ")))
    return items


def update_ledger(content: str, today: str) -> int:
    """Append today's items to the ledger; idempotent on date prefix."""
    new_lines = [
        f"{today}\t{url}\t{headline}"
        for (headline, url) in extract_items(content)
    ]
    existing: list[str] = []
    if LEDGER.exists():
        existing = [
            line for line in LEDGER.read_text().splitlines()
            if line and not line.startswith(f"{today}\t")
        ]
    all_lines = existing + new_lines
    LEDGER.write_text("\n".join(all_lines) + "\n" if all_lines else "")
    return len(new_lines)


# ---- Email rendering ----

def md_to_html(text: str) -> str:
    """Lightweight Markdown -> styled HTML for email."""
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def inline(s: str) -> str:
        s = html.escape(s, quote=False)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2" style="color:#2563eb;text-decoration:underline;">\1</a>',
            s,
        )
        return s

    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            close_list()
            out.append(f'<h1 style="font-size:24px;margin:16px 0;">{inline(s[2:])}</h1>')
        elif s.startswith("## "):
            close_list()
            out.append(
                f'<h2 style="font-size:18px;margin:24px 0 8px;'
                f'border-bottom:1px solid #e5e7eb;padding-bottom:4px;">{inline(s[3:])}</h2>'
            )
        elif s.startswith("- "):
            if not in_list:
                out.append('<ul style="margin:8px 0;padding-left:24px;">')
                in_list = True
            out.append(f'<li style="margin:4px 0;">{inline(s[2:])}</li>')
        elif s == "---":
            close_list()
            out.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0;">')
        elif s == "":
            close_list()
        else:
            close_list()
            out.append(f'<p style="margin:8px 0;">{inline(s)}</p>')
    close_list()
    return "\n".join(out)


def send_email(content: str, today: str, niche_name: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("RESEND_API_KEY not set — skipping email.")
        return

    body_html = md_to_html(content)
    styled = (
        '<!DOCTYPE html>\n'
        '<html><body style="font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',sans-serif;max-width:720px;margin:0 auto;padding:24px;'
        'line-height:1.6;color:#1a1a1a;background:#fff;">\n'
        f"{body_html}\n"
        "</body></html>"
    )
    payload = {
        "from": DIGEST_FROM,
        "to": [DIGEST_TO],
        "subject": f"Cool Topic Readings: {niche_name} — {today}",
        "html": styled,
        "text": content,
    }
    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "morning-digest/1.0",
        },
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    print(f"RESEND OK: {r.json()}")


# ---- Entry point ----

def main() -> int:
    today = today_str()
    niche = niche_for_date(today)
    print(f"=== Cool Topic Readings: {niche['name']} run for {today} ===")

    # Idempotency guard: if today's digest already exists in the repo (committed
    # by an earlier successful fire), exit immediately. This makes the multi-time
    # cron schedule (13:00 / 15:00 / 17:00 UTC) safe — only the first successful
    # slot does work; later slots no-op cleanly.
    digest_path = DIGESTS_DIR / f"{today}.md"
    if digest_path.exists() and digest_path.stat().st_size > 5000:
        print(
            f"Today's digest already exists at {digest_path.relative_to(REPO_ROOT)} "
            f"({digest_path.stat().st_size:,} bytes). Nothing to do."
        )
        return 0

    prompt = render_prompt(today)
    print(f"Rendered prompt: {len(prompt):,} chars")

    digest = run_agent(prompt)
    validate_digest(digest)
    print(f"Digest produced: {len(digest):,} chars")

    path = write_digest(digest, today)
    print(f"Wrote {path.relative_to(REPO_ROOT)}")

    appended = update_ledger(digest, today)
    print(f"Ledger: appended {appended} items")

    send_email(digest, today, niche["name"])
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
