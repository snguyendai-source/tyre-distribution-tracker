# European Tyre Distribution — Daily News Tracker

Checks Google News (free, no API key) every day for 118 named distribution
entities across 11 countries, keeps track of what it's already seen so only
genuinely new mentions show up, and (optionally) emails you a daily digest.
Results are shown on a small dashboard hosted via GitHub Pages.

## Status

This has been built and tested locally (RSS parsing, dedup logic across
consecutive runs, email payload construction, and the dashboard — all
verified against realistic sample data with zero errors). **It is not yet
live** — that needs an empty GitHub repo, which only you can create.

## Setup — do these in order

### 1. Create an empty repository
On GitHub: **New repository** → give it a name (e.g. `tyre-distribution-tracker`)
→ do **not** initialize it with a README, .gitignore, or license (leave it
completely empty) → Create.

### 2. Get this code into it
Two ways — pick whichever you're more comfortable with:

**A. Run these commands yourself** (safest — no credentials touch this chat):
```bash
cd tyre-daily-tracker
git init
git add .
git commit -m "Initial commit: daily tyre distribution news tracker"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git
git push -u origin main
```

**B. Give me a Personal Access Token and I push it for you.**
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
token, scoped to just this one repo, with **Contents: Read and write**
permission. If you'd rather I not touch a credential like this, option A is
just as fast — your call.

### 3. Turn on GitHub Pages
In the repo: **Settings → Pages → Build and deployment → Source: Deploy from
a branch → Branch: main, folder: / (root)** → Save. Your dashboard will be
live at `https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/` within a minute or
two.

### 3b. One placeholder link to fix
`index.html` has a "Run manually on GitHub" button pointing at a placeholder
URL (`github.com/OWNER/REPO/...`). Open the file, replace `OWNER/REPO` with
your actual `your-username/your-repo-name`, and push that one-line change —
or tell me your repo name and I'll do it before you push in step 2.

### 4. (Optional) Set up email digests
Without this, the dashboard still works — you just won't get emails.
- Sign up free at [resend.com](https://resend.com) and grab an API key.
- In the repo: **Settings → Secrets and variables → Actions → New repository
  secret**, add:
  - `RESEND_API_KEY` — your Resend key
  - `ALERT_EMAIL_TO` — where you want the daily digest sent
  - `ALERT_EMAIL_FROM` — optional, defaults to `onboarding@resend.dev` (fine
    for testing; use a verified domain in Resend for production)

### 5. Trigger the first run
The schedule is daily at 06:00 UTC, but don't wait for it — go to the repo's
**Actions** tab → **Daily tyre distribution news check** → **Run workflow**
to trigger it immediately and confirm everything works end to end.

## How it works day to day

- Every day, a GitHub Action runs `scripts/check_news.py`, which checks
  Google News for each entity in `data/entities.json`, figures out what's
  new since the last run (tracked in `data/seen_links.json`), and writes the
  result to `data/latest_run.json` (plus a rolling summary in
  `data/history.json`).
- The Action commits those updated files back to the repo — **that commit
  history is your changelog for free**, no separate log to maintain.
- The dashboard (`index.html`, served by GitHub Pages) just reads those JSON
  files. No backend, no database, no functions.
- To add or remove a monitored entity: edit `data/entities.json` directly on
  GitHub (or locally + push) and the next run picks it up automatically.
- To change the schedule: edit the `cron:` line in
  `.github/workflows/daily-news-check.yml` (currently `0 6 * * *` = daily,
  06:00 UTC).

## What's in here

```
index.html                              — the dashboard
data/entities.json                      — the 118 monitored entities
data/latest_run.json                    — most recent check's results (starts empty)
data/history.json                       — rolling log of past runs (starts empty)
data/seen_links.json                    — dedup state, don't edit by hand
scripts/check_news.py                   — the actual check logic
requirements.txt                        — Python dependencies (feedparser, requests)
.github/workflows/daily-news-check.yml  — the schedule + the commit-back step
```

## Scoping notes worth knowing

- **Pure tyre manufacturers are deliberately excluded** (Michelin, Continental,
  Bridgestone, Goodyear, Pirelli, Nokian, Hankook) — including them would
  flood a distribution-focused digest with generic manufacturer PR that has
  nothing to do with channel structure. Manufacturer-owned *distribution*
  brands (Vergölst, Euromaster, First Stop, BestDrive) are kept, since those
  are genuine channel entities.
- **118 entities**, not the ~130-150 originally estimated — the difference is
  real deduplication: several companies (A.T.U, Pneuhage, Emil Frey, Vianor,
  Norauto, Vulco, Profile Tyrecenter) operate in multiple countries and are
  checked once, not once per country, so they're not checked twice a day for
  the same news.
- Email digests are configured to send **every day regardless of whether
  anything new was found** (per your preference) — a "nothing new today"
  email is still sent so it reads as a routine check-in rather than going
  silent.
