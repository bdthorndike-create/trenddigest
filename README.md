# ZeroHumanEngine / TrendDigest

Fully autonomous, zero-capital content business. Runs itself forever on
GitHub's free tier. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full loop.

## Deploy (one time, ~3 minutes)

1. Create a new **public** GitHub repository.
2. Push this directory to it:
   ```bash
   git init && git add -A && git commit -m "boot: zerohumanengine"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. Repo → Settings → Pages → Build and deployment → Source: **Deploy from a
   branch** → Branch: `gh-pages`, folder `/ (root)`. (The workflow creates
   this branch on the first run.)
4. Optional monetization: Repo → Settings → Secrets and variables → Actions
   → New repository secret → Name `AMAZON_TAG`, value = your Amazon
   Associates tracking ID (e.g. `yourtag-20`). Affiliate buy-links then
   appear automatically on every topic page. Without it the site runs
   normally and the links activate the moment the secret exists.
5. Repo → Actions tab → select "ZeroHumanEngine — autonomous run" →
   Enable workflow. Use **Run workflow** button for an immediate first run.

The site goes live at `https://<you>.github.io/<repo>/` and updates itself
twice a day, forever.

## Local run

```bash
python3 main.py        # no dependencies; Python 3.9+ stdlib only
```

Outputs the static site to `./site` and mutates `state/site_state.json`.

## What the engine does every run
Fetch → score → publish → measure → mutate → commit. Topic weights, keyword
focus, and even the set of topics themselves evolve based on real page views
(GitHub Traffic API) and real story engagement (HN point deltas). Topics that
get attention are amplified; dead topics decay and are retired; new
challenger topics are discovered automatically from trending vocabulary.

## Repo layout
```
main.py                    # the entire engine (stdlib only)
.github/workflows/cron.yml # the 12-hour heartbeat
state/site_state.json     # the mutable brain (committed each run)
site/                     # generated output (deployed to Pages)
ARCHITECTURE.md           # full system design
```
