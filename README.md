# Daily Automotive Sales Signals

A Python script that runs on a schedule, reads public automotive news, removes noise and duplicates, sorts what's left into sales signals, and emails one short report to the whole team at a set local time.

```
GitHub Actions (hourly) → signals.py → email to everyone in config.yaml at send_hour
```

## What's in the repo

| File | What it is |
|---|---|
| `config.yaml` | **Team settings:** recipients, send time, timezone, optional watchlist. The only file most people edit. |
| `rules.yaml` | Sources, noise filters, signal keywords, thresholds. For whoever maintains the report. |
| `signals.py` | The whole workflow, in one file |
| `.github/workflows/daily.yml` | The schedule |
| `sent.json` | What was already emailed (created by the first send, kept up to date by the workflow) |
| `tests/` | Tests, run against saved real feeds, with no network and no email |
| `docs/design.md` | Workflow design, explanation and assumptions |
| `samples/` | A sample email built from live feeds |

## Configure

```yaml
recipients:                # everyone gets the same email; any address works (Gmail, Outlook, lists)
  - sales-team@example.com
send_hour: 8               # local hour, 0-23
timezone: America/Chicago

watchlist:                 # optional: companies or brands, aliases after a bar
  - AutoNation | AutoNation USA
  - Toyota
```

Without a watchlist, the email has three sections: **New business**, **Industry development** and **Lower priority**. With one, news about watchlist names also appears under **Potential risk** and **Potential growth**.

## Set up email (Gmail app password)

1. Use a Gmail or Google Workspace account to send from. Turn on 2-step verification for it; app passwords require it.
2. Go to https://myaccount.google.com/apppasswords, create an app password, and copy the 16 characters.
3. Use that account as `SMTP_USER` and the app password as `SMTP_PASS`.

Other providers work too: set `SMTP_HOST` and `SMTP_PORT` (465 for SSL, 587 for STARTTLS).

## Run it on GitHub (free)

1. Push this folder to a GitHub repository.
2. **Settings → Secrets and variables → Actions → New repository secret:** add `SMTP_USER` and `SMTP_PASS`. Optional: `ANTHROPIC_API_KEY` for AI tagging, and `MAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`.
3. **Actions → Daily sales signals → Run workflow → mode: preview.** This builds the email without sending it; download it from the run's *email-preview* artifact.
4. To check delivery, run it again with **mode: send-now** and your address in **to**. Only you get the copy, and it doesn't count as the day's email.

After that it runs by itself. Every hour at :17 (UTC) the job checks the time in `config.yaml`'s timezone, and sends once a day at `send_hour`. Changing the time or timezone needs no edit to the workflow.

**Cost:** about 1 minute per run, roughly 720 minutes a month. That's free on public repositories and within the 2,000 free minutes a month for private ones.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in `SMTP_USER` and `SMTP_PASS` in `.env`.

```bash
.venv/bin/python signals.py --preview
```

| Command | What it does |
|---|---|
| `python signals.py` | Scheduled mode: sends only at `send_hour`, once a day |
| `python signals.py --preview` | Builds the email into `out/preview.html` and `out/preview.txt`; sends nothing |
| `python signals.py --send-now` | Builds and sends now, ignoring the clock |
| `python signals.py --send-now --to me@example.com` | Sends a test copy to one address |

Tests:

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest
```

## How it works

1. **Collect.** 43 free public sources, fetched in parallel with a timeout and retries: trade-press RSS feeds (CBT News, Automotive Dive, Motor Trader, Autocar Professional and others), press-release wires, the Federal Register, SMMT, the NHTSA recalls dataset, regional business news, and 13 Google News searches. Only headlines and feed summaries are used; nothing is scraped or republished.
2. **Clean.** Tracking parameters are stripped from links and HTML from text. Noise is dropped: reviews, motorsport, deals, digests, spy shots. General-news feeds must also look automotive.
3. **Remove duplicates.** Three checks: the same link; the same story from another outlet (similar headline, or the same signal about the same companies); anything emailed in the last 14 days. The most credible source is kept: press release first, then trade press, then general news.
4. **Tag.** With `ANTHROPIC_API_KEY` set, Claude Haiku 5.5 reads stories in batches of 20 and extracts the signal, the companies and their roles (who bought whom), a factual summary, an implication and a confidence score. Names it returns are checked against the article text. If the AI is off or a batch fails, keyword rules from `rules.yaml` tag that batch instead.
5. **Route.** A fixed table decides each story's section from its signal and whether it involves a watchlist name. For example, a watched company buying stores is *Potential growth* and being bought is *Potential risk*; any other acquisition is *New business*. Low-confidence items drop to *Industry development*. A company with two or more stories in 30 days is marked **Priority**.
6. **Email.** One template: each item is the fact with its source linked, then the implication on its own line. Empty sections are left out. A quiet day sends one line. After a missed day, the next email covers everything since the last one.

## When something goes wrong

| Problem | What happens |
|---|---|
| A source is down | Retried, then skipped; the run continues |
| Most sources are down, or AI tagging fell back to rules | The email still goes out, with a "Coverage was partial today" line |
| Sending fails | The job fails, and GitHub emails the repository owner (the alert). The next hourly run retries, for up to 3 hours after `send_hour`. |
| The scheduler misses a run, or runs twice | The next run catches up; `sent.json` prevents a second email the same day |

Make sure GitHub's failure notifications are on (**Settings → Notifications → Actions**). Each run's log shows the counts at every step.

## Assumptions

- Free, public, English sources only. Coverage is strongest for North America, India and the UK.
- One report for the whole team; no per-person filtering.
- The report says what an event typically means. When and how to act is the reader's call.
- Credentials live only in GitHub secrets or a local `.env` file, never in the repository.
- Thresholds in `rules.yaml` (headline similarity 0.75, confidence gate 0.7, section caps) are starting values, to be tuned after the first weeks.
- GitHub may start scheduled jobs a few minutes late, so the email arrives a little after the hour.
