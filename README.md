# Daily Automotive Sales Signals

Every morning at 8:00 AM IST, Tekion's sales team gets one short email covering the automotive news that matters for selling: dealer acquisitions, new stores, leadership changes, competitor moves and industry shifts. The noise and the repeats are already gone.

```
GitHub Actions (hourly) → signals.py → one email to the team at send_hour
```

## What the email looks like

Four sections, in order: **Industry trends**, **Sales signals**, **Competitors**, **Tekion**. Each story gets one line:

> **Company · Region** – a one-sentence summary (outlet link)
> *Pitch:* the need this event creates and the Tekion product that meets it (Sales signals only)

## How it works

1. **Collect** from 55 free public sources: trade-press RSS, Google News searches, Exa web searches and NHTSA recalls.
2. **Normalize and filter noise:** clean titles and URLs, then drop reviews, motorsport, bikes and blogs with fixed rules.
3. **Deduplicate:** the same URL, or a near-identical headline from another outlet.
4. **AI classification** (Claude Haiku 5.5): signal type, companies, region, summary and pitch.
5. **Validate AI output:** the company names must appear in the article, and the story must be current and on topic.
6. **Event-level dedupe** (same signal and companies, different headlines), then **route and rank** into sections with fixed rules.
7. **Compose and send** from a template over SMTP, then log the run in `sent.json`.

AI is used only in step 4. If it can't read a story, the story is left out rather than guessed at, and the email says coverage was partial.

## Docs

| Doc | What's in it |
|---|---|
| [docs/workflow-design.md](docs/workflow-design.md) | Assumptions, personas, problems and solutions, trade-offs, the workflow and metrics |
| [docs/setup-and-execution.md](docs/setup-and-execution.md) | The accounts you need, how to put the code on GitHub, add the keys, test and run it |
| [samples/](samples/) | A sample email |

## Quick start

1. Fork this repo, then turn on **Actions** in your fork.
2. Add the secrets: `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` and `ANTHROPIC_API_KEY`, plus `EXA_API_KEY` if you want it. This build sends through **Brevo**; any SMTP provider works.
3. Put your recipients in `config.yaml`.
4. Go to **Actions → Daily sales signals → Run workflow**. Use `preview` first, then `send-now` to your own address.

The full steps are in [docs/setup-and-execution.md](docs/setup-and-execution.md).

## What's in the repo

| File | What it is |
|---|---|
| `config.yaml` | Recipients, send time, timezone. The only file most people edit |
| `rules.yaml` | Sources, competitors, Tekion products, junk filters, thresholds |
| `signals.py` | The whole workflow, in one file |
| `.github/workflows/daily.yml` | The hourly schedule |
| `sent.json` | Run log, which also stops a second email on the same day |
| `tests/` | 61 tests. They use no network and send no email |
