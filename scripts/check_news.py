#!/usr/bin/env python3
"""
Daily news check for the European Tyre Distribution Tracker.

For each named entity in data/entities.json, queries Google News' public RSS
feed, keeps only articles not seen on a previous run (tracked in
data/seen_links.json), writes the results to data/latest_run.json, appends a
summary to data/history.json, and -- if RESEND_API_KEY and ALERT_EMAIL_TO are
set -- emails a daily digest via Resend's HTTP API.

Run with: python scripts/check_news.py
"""
import json
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ENTITIES_PATH = DATA_DIR / "entities.json"
SEEN_PATH = DATA_DIR / "seen_links.json"
LATEST_RUN_PATH = DATA_DIR / "latest_run.json"
HISTORY_PATH = DATA_DIR / "history.json"

MAX_ITEMS_PER_ENTITY = 8
MAX_SEEN_LINKS = 8000
MAX_HISTORY_RUNS = 60
CONCURRENCY = 10
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; TyreDistributionTracker/1.0; +daily-check)"


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def news_feed_url(search_query: str) -> str:
    q = urllib.parse.quote(search_query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_entity_news(entity: dict) -> list[dict]:
    """Fetch and return up to MAX_ITEMS_PER_ENTITY items for one entity.
    Returns [] on any failure -- a single entity's feed failing should never
    take down the whole run."""
    url = news_feed_url(entity["search_query"])
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries[:MAX_ITEMS_PER_ENTITY]:
            items.append({
                "title": getattr(entry, "title", "(untitled)"),
                "link": getattr(entry, "link", ""),
                "pubDate": getattr(entry, "published", ""),
            })
        return items
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"  [warn] fetch failed for {entity['name']!r}: {exc}", file=sys.stderr)
        return []


def build_email_html(new_items_by_entity: dict, run_at: str, entities_checked: int) -> str:
    entries_with_news = len(new_items_by_entity)
    total_new = sum(len(v) for v in new_items_by_entity.values())
    if not new_items_by_entity:
        body = "<p>No new mentions found today.</p>"
    else:
        blocks = []
        for entity_name, items in new_items_by_entity.items():
            links = "".join(
                f'<li style="margin-bottom:4px;">'
                f'<a href="{item["link"]}" style="color:#2F6F62;">{_escape(item["title"])}</a>'
                f'<br><span style="color:#5B6478;font-size:12px;">{_escape(item.get("pubDate",""))}</span></li>'
                for item in items
            )
            blocks.append(
                f'<h3 style="margin:18px 0 6px;font-size:15px;color:#16233A;">{_escape(entity_name)}</h3>'
                f'<ul style="margin:0 0 4px;padding-left:18px;">{links}</ul>'
            )
        body = "".join(blocks)

    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;">
  <h2 style="color:#16233A;">Tyre distribution — daily news digest</h2>
  <p style="color:#5B6478;font-size:13px;">Check completed {_escape(run_at)}.
     {entries_with_news} of {entities_checked} monitored entities had new mentions
     ({total_new} article{'s' if total_new != 1 else ''} total).</p>
  {body}
  <p style="color:#9AA2B1;font-size:11px;margin-top:24px;">Automated daily check — European Tyre Distribution Tracker.</p>
</div>"""


def _escape(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def send_digest_email(html: str, total_new: int) -> None:
    resend_key = os.environ.get("RESEND_API_KEY")
    to_email = os.environ.get("ALERT_EMAIL_TO")
    from_email = os.environ.get("ALERT_EMAIL_FROM", "Tyre Tracker <onboarding@resend.dev>")

    if not resend_key or not to_email:
        print("Email not configured (RESEND_API_KEY / ALERT_EMAIL_TO missing) — skipping email step.")
        return

    subject = f"Tyre distribution news — {total_new} new mention{'s' if total_new != 1 else ''}" \
        if total_new else "Tyre distribution news — daily check-in, nothing new today"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={"from": from_email, "to": [to_email], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 300:
            print(f"  [warn] Resend API returned {resp.status_code}: {resp.text}", file=sys.stderr)
        else:
            print(f"Digest email sent to {to_email}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] email send failed: {exc}", file=sys.stderr)


def main() -> None:
    entities = load_json(ENTITIES_PATH, [])
    if not entities:
        print("No entities found in data/entities.json — aborting.", file=sys.stderr)
        sys.exit(1)

    seen = load_json(SEEN_PATH, {})

    new_items_by_entity: dict[str, list[dict]] = {}
    results_lock_items = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        future_to_entity = {pool.submit(fetch_entity_news, e): e for e in entities}
        for future in as_completed(future_to_entity):
            entity = future_to_entity[future]
            items = future.result()
            fresh = [it for it in items if it["link"] and it["link"] not in seen]
            if fresh:
                for it in fresh:
                    seen[it["link"]] = True
                new_items_by_entity[entity["name"]] = [
                    {**it, "country": entity["countries"][0], "cluster": entity["cluster"]}
                    for it in fresh
                ]

    run_at = datetime.now(timezone.utc).isoformat()
    total_new = sum(len(v) for v in new_items_by_entity.values())

    # Cap the seen-links set so it doesn't grow forever.
    if len(seen) > MAX_SEEN_LINKS:
        # Keep an arbitrary-but-stable tail; dict insertion order is preserved in Python 3.7+.
        seen = dict(list(seen.items())[-MAX_SEEN_LINKS:])
    save_json(SEEN_PATH, seen)

    latest_run = {
        "runAt": run_at,
        "totalNew": total_new,
        "entitiesChecked": len(entities),
        "entitiesWithNews": len(new_items_by_entity),
        "newItemsByEntity": new_items_by_entity,
    }
    save_json(LATEST_RUN_PATH, latest_run)

    history = load_json(HISTORY_PATH, [])
    history.insert(0, {
        "runAt": run_at,
        "totalNew": total_new,
        "entitiesWithNews": len(new_items_by_entity),
    })
    save_json(HISTORY_PATH, history[:MAX_HISTORY_RUNS])

    print(f"Done: {total_new} new item(s) across {len(new_items_by_entity)} entit(y/ies), "
          f"{len(entities)} checked.")

    html = build_email_html(new_items_by_entity, run_at, len(entities))
    send_digest_email(html, total_new)


if __name__ == "__main__":
    main()
