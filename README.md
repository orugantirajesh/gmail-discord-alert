# Gmail → Discord Alert

Scans a Gmail inbox hourly and posts a Discord alert only when something
genuinely important shows up — an invoice/payment due, an urgent email from a
real person, or a security alert (new sign-in, password reset, etc.). Stays
silent when nothing qualifies, by design.

## How it works

```
cron (hourly, :50) → run.sh → scan_and_alert.py
                                 ├─ IMAP (Gmail, read-only) → fetch last hour's emails
                                 ├─ Claude (Sonnet 5)        → classify importance
                                 └─ Discord webhook           → post alert if anything qualifies
```

Runs as a plain cron job on a Linux VPS — no Docker, no external platform
dependency. This was deliberately moved off Anthropic's hosted scheduled-
routines feature (`/schedule` cloud agents) because that sandbox's network
policy blocks outbound requests to `discord.com`; a VPS with normal internet
access has no such restriction.

## Why IMAP instead of the Gmail API / MCP connector

The Gmail MCP connector used by Claude's cloud routines is tied to an
interactive claude.ai login session — it's not something a standalone cron
job can authenticate as. IMAP with a Gmail **App Password** (not your real
password, 2FA-gated, revocable independently) is the simplest way to get
read-only inbox access from a script running unattended on a server.

Search uses Gmail's `X-GM-RAW` IMAP extension so the script can reuse native
Gmail search syntax (e.g. `newer_than:1h`) instead of IMAP's own `SINCE`
criterion, which only has date granularity, not time-of-day.

## Required environment (`.env` on the server — never committed)

```
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx      # myaccount.google.com/apppasswords (needs 2FA on)
ANTHROPIC_API_KEY=sk-ant-...             # pay-per-use key from console.anthropic.com — NOT a Pro/Max subscription
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
SCAN_WINDOW=newer_than:1h                 # optional, Gmail search syntax
```

**On the Anthropic API key:** this must be a classic pay-per-use API key, not
subscription (Pro/Max) credentials. Anthropic's terms prohibit third-party
tools from routing requests through subscription credentials, and it's
blocked server-side as of Jan 2026 regardless.

## Deploying an update

```bash
./deploy.sh
```

Copies `scan_and_alert.py` to the VPS and leaves everything else (`.env`,
`venv/`, cron, logrotate) untouched. See `deploy.sh` for the exact target.

## Gotchas hit during development (worth knowing before touching this again)

1. **Discord returns 403 to Python's `urllib` but not to `curl`.** Discord's
   edge filters the default `Python-urllib/x.y` User-Agent string. The script
   sets an explicit `User-Agent` header — don't remove it.
2. **IMAP's `SINCE` has no time-of-day granularity** — it's date-only. Use
   `X-GM-RAW` with Gmail's own query syntax instead (already done).
3. **The "Received" timestamp is computed in Python, not by the LLM.** Email
   `Date` headers carry whatever timezone the sender's mail client used
   (frequently UTC/GMT); the script parses it with `email.utils.parsedate_to_datetime`
   and converts to `Australia/Sydney` via `zoneinfo`, which also handles the
   AEST/AEDT daylight-saving switch automatically. Don't ask the model to
   reformat timestamps — it won't do so reliably.
4. **This runs read-only against Gmail.** It never replies, archives,
   deletes, labels, or marks anything read — only reads + posts to Discord.
   Keep it that way; don't add write scopes without a good reason.

## Operational notes

- Logs: `/opt/gmail-discord-alert/logs/run.log` on the VPS (weekly rotation,
  4 weeks kept, via `/etc/logrotate.d/gmail-discord-alert`)
- Cron: `crontab -l` on the VPS (`50 * * * *`)
- Manual test run: `ssh root@<vps-ip> /opt/gmail-discord-alert/run.sh`
