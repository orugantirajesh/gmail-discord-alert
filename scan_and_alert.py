#!/usr/bin/env python3
"""Hourly Gmail -> Discord alert scanner.

Connects to Gmail over IMAP (app password), pulls emails from the last
scan window via Gmail's X-GM-RAW IMAP extension (so we can reuse Gmail's own
search syntax, e.g. "newer_than:1h"), classifies them with Claude, and posts
a single Discord embed message if anything qualifies. Silent (no Discord
post) when nothing qualifies, by design -- this runs unattended via cron and
should not spam the channel.

Required environment variables (see .env, loaded by the wrapper shell script):
  GMAIL_ADDRESS        e.g. haioruganti@gmail.com
  GMAIL_APP_PASSWORD   16-character Gmail App Password (IMAP access only)
  ANTHROPIC_API_KEY    pay-per-use API key (separate from any Claude subscription)
  DISCORD_WEBHOOK_URL  the channel webhook to post alerts to
  SCAN_WINDOW          optional, default "newer_than:1h" (Gmail search syntax)
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

import anthropic

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SCAN_WINDOW = os.environ.get("SCAN_WINDOW", "newer_than:1h")
MODEL = os.environ.get("CLASSIFY_MODEL", "claude-sonnet-5")

CATEGORY_COLORS = {
    "invoice_payment": 16096036,  # amber
    "urgent_client": 15158332,  # red
    "security": 3447003,  # blue
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def to_sydney_time(date_header: str | None) -> str:
    """Parse an email Date header and format it in Australia/Sydney local time.

    Handles the AEST/AEDT daylight-saving switch automatically via zoneinfo.
    Falls back to the raw header text if it can't be parsed, rather than
    raising -- a formatting miss shouldn't break the whole alert.
    """
    if not date_header:
        return "unknown"
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:  # some senders omit an offset; assume UTC
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(SYDNEY_TZ)
        return local.strftime("%-d %b %Y, %-I:%M %p %Z")  # e.g. "7 Oct 2026, 9:35 pm AEDT"
    except (TypeError, ValueError):
        return date_header


def extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


def fetch_recent_emails() -> list[dict]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)  # never mutate the mailbox
        query = f'"{SCAN_WINDOW} in:inbox"'
        status, data = imap.search(None, "X-GM-RAW", query)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        emails = []
        for eid in ids:
            status, msg_data = imap.fetch(eid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            raw_date = msg.get("Date", "")
            emails.append(
                {
                    "subject": decode_mime(msg.get("Subject")),
                    "sender": decode_mime(msg.get("From")),
                    "date": raw_date,
                    "received_sydney": to_sydney_time(raw_date),
                    "body": extract_body(msg)[:2000],
                }
            )
        return emails
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


CLASSIFY_PROMPT = """You are classifying emails for an inbox-alert system. For each email below, decide if it qualifies as IMPORTANT, and if so, which category:

1. invoice_payment -- invoice, amount due, payment due, overdue, past due, pay now, bill, subscription renewal charge, outstanding balance.
2. urgent_client -- a time-sensitive email from a real person/organization (not automated marketing) asking for a reply/decision/action with time pressure. NOT a newsletter, promo, or routine automated notification.
3. security -- login alerts, password resets, suspicious sign-in activity, account-security notices. If it looks like phishing itself, still flag it but note "possible phishing" in the description.

EXCLUDE: marketing/promotional email, newsletters, social notifications, already-completed transaction confirmations with nothing owing/pending (e.g. "your payment went through", "order confirmed").

Respond with ONLY a JSON array, no other text, no markdown fences. One object per email that QUALIFIES (omit any that don't qualify). Do NOT include a "received" field -- that's attached separately:
[{{"index": 0, "category": "invoice_payment", "title": "short title", "description": "1-2 sentence summary with amount/due date if known", "sender": "..."}}]

If none qualify, respond with exactly: []

EMAILS:
{emails_json}
"""


def classify(emails: list[dict]) -> list[dict]:
    if not emails:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # received_sydney is deliberately omitted here -- the model doesn't need it
    # to classify, and we attach the Python-computed value below rather than
    # trust the model to copy a timestamp through unmangled.
    payload = [
        {"index": i, "subject": e["subject"], "sender": e["sender"], "date": e["date"], "body": e["body"]}
        for i, e in enumerate(emails)
    ]
    prompt = CLASSIFY_PROMPT.format(emails_json=json.dumps(payload, indent=2))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        qualifying = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            raise RuntimeError(f"Could not parse classification response: {text[:300]!r}")
        qualifying = json.loads(text[start : end + 1])

    # Attach the Sydney-local time computed in fetch_recent_emails(), keyed by
    # the index the model echoed back -- never let the model invent this field.
    for item in qualifying:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(emails):
            item["received"] = emails[idx]["received_sydney"]
        else:
            item["received"] = "unknown"
    return qualifying


def post_to_discord(qualifying: list[dict]) -> None:
    if not qualifying:
        return
    embeds = []
    for item in qualifying[:10]:
        embeds.append(
            {
                "title": str(item.get("title", "Untitled"))[:256],
                "description": str(item.get("description", ""))[:2048],
                "color": CATEGORY_COLORS.get(item.get("category"), 0x5865F2),
                "fields": [
                    {"name": "From", "value": str(item.get("sender", "unknown"))[:256], "inline": True},
                    {"name": "Category", "value": str(item.get("category", "unknown")), "inline": True},
                    {"name": "Received", "value": str(item.get("received", ""))[:256], "inline": True},
                ],
            }
        )
    body = json.dumps({"username": "Gmail Alerts", "embeds": embeds}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=body,
        headers={
            # Discord's edge filters urllib's default "Python-urllib/x.y" UA.
            # curl's UA sails through fine -- a real UA string does too.
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; GmailDiscordAlert/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord post failed: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord post failed: HTTP {exc.code} - {detail[:500]}") from exc


def main() -> None:
    emails = fetch_recent_emails()
    log(f"fetched {len(emails)} email(s) in window '{SCAN_WINDOW}'")
    qualifying = classify(emails)
    log(f"{len(qualifying)} qualifying")
    post_to_discord(qualifying)
    if qualifying:
        titles = "; ".join(f"[{q.get('category')}] {q.get('title')}" for q in qualifying)
        log(f"Posted {len(qualifying)} alert(s) to Discord: {titles}")
    else:
        log("No qualifying emails this run")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level cron entrypoint
        log(f"ERROR: {exc}")
        sys.exit(1)
