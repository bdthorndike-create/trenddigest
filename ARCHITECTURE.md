# ZeroHumanEngine — System Architecture

A fully autonomous, zero-capital online business: a programmatic-SEO tech
digest site ("TrendDigest") that fetches, curates, publishes, monetizes, and
optimizes itself with no human in the loop.

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                     GITHUB ACTIONS (free tier)                   │
 │  cron.yml fires every 12h (05:10 & 17:10 UTC) indefinitely        │
 └──────────────────────────────┬───────────────────────────────────┘
                                │ runs
                                ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  main.py — the engine (Python stdlib only, zero dependencies)    │
 │                                                                  │
 │  1. LOAD STATE      state/site_state.json  (the "brain": topic  │
 │                     weights, posted-story ledger, generation #) │
 │                                                                  │
 │  2. ACQUIRE         Hacker News (Algolia) API  — top stories     │
 │                     Dev.to public API           — community     │
 │                                              articles          │
 │                     GitHub Search API          — trending repos │
 │                     (all free, no key required)                 │
 │                                                                  │
 │  3. SCORE + SELECT Engagement-weighted ranking per topic;        │
 │                     dedup against posted-story ledger           │
 │                                                                  │
 │  4. PUBLISH         Generates the static site into ./site:      │
 │                     daily digest homepage, one long-tail SEO    │
 │                     landing page per topic (/t/<topic>/),       │
 │                     archive, sitemap.xml, rss.xml, robots.txt   │
 │                                                                  │
 │  5. MONETIZE        Amazon Associates search links auto-        │
 │                     injected on every topic page (AMAZON_TAG    │
 │                     secret; system runs fine without it and    │
 │                     activates links the moment the tag exists)  │
 │                                                                  │
 │  6. SELF-CORRECT ────────────────────────────────────────────────┤
 │     a. GitHub Pages Traffic API  → which topic pages humans      │
 │        actually view → boost those topics (up to 2x per run)     │
 │     b. HN Algolia re-poll        → stories that gained 25+      │
 │        points since publication → boost their topics (1.15x)     │
 │     c. Keyword mutation          → mine co-occurring terms in   │
 │        top titles → spawn challenger topics at low weight       │
 │     d. Natural selection          → 0.97 decay per run, retire   │
 │        topics under 0.15 weight, normalize the distribution     │
 │     e. Bump generation counter, append to run history          │
 │                                                                  │
 │  7. PERSIST         Mutated state committed back to the repo    │
 │                     (Contents API + git push) — this also keeps  │
 │                     the repo "active" so GitHub never disables   │
 │                     the scheduled workflow: the loop is          │
 │                     self-sustaining by design.                   │
 └──────────────────────────────┬───────────────────────────────────┘
                                │ deploys ./site
                                ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  GITHUB PAGES (free) → https://<owner>.github.io/<repo>/          │
 │  traffic flows back into step 6a on the next run — closed loop    │
 └─────────────────────────────────────────────────────────────────┘
```

## Cost structure
Every component is free tier: GitHub Actions (2,000 min/month public repos —
each run uses ~1 minute), GitHub Pages (unlimited public sites), HN Algolia
API (free, keyless), GitHub Search API (free, 10 req/min unauthenticated).
Total fixed cost: $0.

## Why this niche
Developer/tech content: the target audience (a) browses from work machines,
(b) clicks through to Amazon books at a measurable rate, and (c) the entire
topic space is machine-parseable from free structured APIs — no brittle
HTML scraping anywhere in the pipeline.

## Failure modes (all non-fatal by design)
- API outage → `retry()` with backoff, run aborts cleanly, next cron recovers.
- Publish failure → caught, logged, never kills the run.
- Bad state → JSON decode guard resets to seed topics.
