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
LOOKBACK_DAYS_DIGESTS = 2

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


# ---- Helpers ----

def today_str() -> str:
    """Today's date in America/New_York (approximate via fixed offset)."""
    return datetime.now(timezone(timedelta(hours=ET_OFFSET_HOURS))).strftime("%Y-%m-%d")


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


def build_recent_digests(today: str) -> str:
    """Return concatenated content of the last N digest files (excluding today)."""
    if not DIGESTS_DIR.exists():
        return "(no recent digests — first run)"
    files = sorted(
        [p for p in DIGESTS_DIR.glob("[0-9]*.md") if p.stem != today],
        reverse=True,
    )[:LOOKBACK_DAYS_DIGESTS]
    if not files:
        return "(no recent digests — first run)"
    sections = [f"=== {p.stem} ===\n{p.read_text()}" for p in files]
    return "\n\n".join(sections)


def render_prompt(today: str) -> str:
    template = PROMPT_FILE.read_text()
    return (
        template
        .replace("{TODAY}", today)
        .replace("{COVERED_URLS}", build_covered_urls(today))
        .replace("{RECENT_DIGESTS_CONTENT}", build_recent_digests(today))
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
    # Sanity: at least 4 items with a URL across the whole digest.
    # Allow headline and URL to be separated by up to ~600 chars (multi-line items).
    bold_url_pattern = re.compile(
        r"\*\*[^*]{1,200}\*\*.{0,600}?\[[^\]]+\]\(https?://", re.DOTALL
    )
    matches = bold_url_pattern.findall(text)
    if len(matches) < 4:
        raise RuntimeError(
            f"Digest has only {len(matches)} items with URLs (need >=4) — agent "
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


def send_email(content: str, today: str) -> None:
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
        "subject": f"Cool Topic Readings — {today}",
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
    print(f"=== Cool Topic Readings run for {today} ===")

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

    send_email(digest, today)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
