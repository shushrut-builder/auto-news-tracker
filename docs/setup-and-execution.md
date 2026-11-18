# Setup and execution instructions

The report runs on GitHub Actions, so there's no server to look after. Setup takes about 20 minutes: create the accounts, put the code on GitHub, add the keys, test, and leave it running.

## 1. Accounts you need

| Account | Needed? | What it's for | Cost |
|---|---|---|---|
| **GitHub** (github.com) | Yes | Stores the code and runs it every hour | Free |
| **An email-sending account** | Yes | Sends the report. This build uses **Brevo**; Gmail, Microsoft 365 or your company's SMTP server also work (see step 3) | Brevo's free plan sends 300 emails a day |
| **Anthropic** (console.anthropic.com) | Recommended | The Claude API key that reads and summarises each story | Pay per use; add a few dollars of credit |
| **Exa** (exa.ai) | Optional | 10 extra web searches aimed at sales signals | Free starter credits |

**Setting up Brevo** (what this build uses):
1. Sign up at brevo.com.
2. Add and verify the address the email should come from, in Brevo's **Senders** settings. This is your `MAIL_FROM`.
3. Open **SMTP & API → SMTP**. Note the **SMTP login** (this is `SMTP_USER`) and generate an **SMTP key** (this is `SMTP_PASS`).
4. The server is `smtp-relay.brevo.com`, port `587`.

**Getting the Claude key:** sign in at console.anthropic.com, add credit under **Billing**, then create a key under **API keys**. Copy it straight away; it's shown only once.

**On your computer** you only need Git, plus Python 3.12 or later if you want to run the tests or the report locally.

## 2. Put the code on your GitHub

The code is at https://github.com/shushrut-builder/auto-news-tracker. Pick one way.

**Option A: fork it (easiest, all in the browser)**
1. Open the link above and click **Fork**, then **Create fork**.
2. In your fork, open the **Actions** tab and click **I understand my workflows, go ahead and enable them**. Forks start with Actions turned off, so the report won't run until you do this.

**Option B: push it to a new repository of your own**
1. On GitHub, click **New repository**. Give it a name, leave it empty (no README), and create it.
2. On your computer, get the code and push it:

```bash
git clone https://github.com/shushrut-builder/auto-news-tracker.git
```

```bash
cd auto-news-tracker
```

```bash
git remote set-url origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
```

```bash
git push -u origin main
```

Git asks you to sign in to GitHub the first time you push. Actions is on by default in a new repository.

When you change something later, push it the same way:

```bash
git add -A
```

```bash
git commit -m "Describe the change"
```

```bash
git pull --rebase
```

```bash
git push
```

Run `git pull --rebase` before every push: the job commits its run log (`sent.json`) back to the repository after each run, so GitHub usually has a newer commit than your computer.

## 3. Add the keys as secrets

In your repository: **Settings → Secrets and variables → Actions → New repository secret.** Add each one. Keys live only here, never in the code.

| Secret | Needed? | What to put in it |
|---|---|---|
| `SMTP_USER` | Yes | The login for your email provider |
| `SMTP_PASS` | Yes | Its password, app password or SMTP key |
| `SMTP_HOST` | Yes, unless you use Gmail | The provider's server. Defaults to `smtp.gmail.com` |
| `SMTP_PORT` | Yes, unless you use Gmail | `587` (STARTTLS) or `465` (SSL, the default) |
| `MAIL_FROM` | If it differs from the login | The "From" address. For Brevo, the sender you verified |
| `ANTHROPIC_API_KEY` | Recommended | The Claude key. Without it, keyword rules sort the stories and the email has headlines instead of summaries, regions and pitches |
| `EXA_API_KEY` | Optional | The Exa key |

**Which values for which provider:**

| Provider | `SMTP_HOST` | `SMTP_PORT` | `SMTP_USER` and `SMTP_PASS` |
|---|---|---|---|
| **Brevo** (what this build uses) | `smtp-relay.brevo.com` | `587` | The SMTP login and SMTP key from Brevo |
| **Your company's SMTP server** (best inside Tekion) | Ask IT | Usually `587` | The service account IT gives you |
| **Gmail or Google Workspace** | `smtp.gmail.com` | `465` | Your address, plus an app password |
| **Microsoft 365 or Outlook** | `smtp.office365.com` | `587` | Your address, plus an app password |

**App passwords** let a script send mail without your normal password:
- **Gmail:** turn on 2-step verification, open https://myaccount.google.com/apppasswords, create one and use the 16 characters as `SMTP_PASS`.
- **Microsoft 365 or Outlook:** turn on multi-factor sign-in, then create an app password in your account's security settings. On a work account, your admin may need to allow SMTP sending first.

## 4. Say who gets it and when

Edit `config.yaml`. You can do this on GitHub itself: open the file, click the pencil icon, then **Commit changes**.

```yaml
recipients:
  - sales-team@example.com   # everyone gets the same email; a team list works
send_hour: 8                 # local hour, 0 to 23
timezone: Asia/Kolkata       # any timezone name, e.g. America/Chicago
contact: Shushrut            # named in the footer for feedback
```

Sources, competitors, Tekion products and the junk filters are in `rules.yaml`. Most people never need to change them.

## 5. Test it

Test in this order; each step checks one more piece.

**a. The code (optional, on your computer, no keys needed).** From the repository folder:

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead. All 61 tests should pass. They use no network and send no email.

**b. The full run, without sending.** On GitHub: **Actions → Daily sales signals → Run workflow**, set mode to `preview`, click **Run workflow**. When it finishes (a few minutes), open the run and download the **email-preview** artifact. It holds the email exactly as it would go out. This proves the sources and the Claude key work.

**c. The email.** Run the workflow again with mode `send-now` and your own address in **to**. Only you get it, and it doesn't count as the day's email. This proves the email settings work. If it doesn't arrive, check spam, then the run's log for the error.

**What a run log tells you:** each run prints how many stories were left after every step (collected, filtered, de-duplicated, tagged, sent), and any source or AI errors.

## 6. Run it

**Automatically.** Nothing more to do. GitHub starts the job every hour. It sends once a day at `send_hour` in your timezone and does nothing on the other runs. If sending fails, it retries immediately, up to 3 tries. If all 3 fail, the next two hourly runs are the backup (with `send_hour: 8`, at 9 and 10 AM).

**By hand.** **Actions → Daily sales signals → Run workflow**, mode `send-now`, **to** left empty. It sends to everyone in `config.yaml` straight away.

**Keeping an eye on it:**
- Turn on failure emails: your GitHub profile → **Settings → Notifications → Actions**.
- `sent.json` in the repository records every run: when, how many stories, and any errors.
- If coverage was partial (the AI couldn't read some stories), the email says so at the top.

**Pausing it:** **Actions → Daily sales signals → ⋯ → Disable workflow.** Enable it again the same way.

**Cost:** about a minute of GitHub Actions per hourly run, which fits in GitHub's free minutes for a public repository. Claude and Exa bill per use; Brevo's free plan covers a daily email.

## 7. Run it on your own computer (optional)

Set up Python as in step 5a. Then copy `.env.example` to `.env` and fill in the same values as the secrets in step 3. Then:

| Command | What it does |
|---|---|
| `python signals.py --preview` | Builds the email into `out/preview.html` and `out/preview.txt`; sends nothing |
| `python signals.py --send-now --to you@example.com` | Sends a test copy to one address |
| `python signals.py --send-now` | Sends to everyone in `config.yaml` now |
| `python signals.py` | Scheduled mode: sends only at `send_hour`, once a day |
