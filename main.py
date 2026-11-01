#!/usr/bin/env python3
"""
ZeroHumanEngine — fully autonomous, zero-capital content business.

Pipeline (runs unattended on GitHub Actions cron, twice daily):
  1. Load evolving topic/keyword state (state/site_state.json).
  2. Pull fresh signals from free public APIs (Hacker News/Algolia, GitHub Search).
  3. Score and select the best-performing content per topic.
  4. Generate a programmatic-SEO static site (site/ directory).
  5. SELF-CORRECTION: read public metrics (GitHub Pages traffic API, HN story
     performance) and rewrite the topic weights / spawn new topics.
  6. Persist the mutated state back to the repo via the GitHub Contents API.

Zero third-party dependencies: Python 3 standard library only.
Monetization: Amazon Associates links (AMAZON_TAG secret) + sponsor slots.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html import escape as e
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (all optional; system degrades gracefully without secrets)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
STATE_FILE = ROOT / "state" / "site_state.json"
RUN_LOG = ROOT / "state" / "run_log.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # owner/name, set by Actions
AMAZON_TAG = os.environ.get("AMAZON_TAG", "")  # Amazon Associates tag
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # public canonical origin, e.g. https://user.github.io/repo

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
GH_SEARCH = "https://api.github.com/search/repositories"
DEVTO_API = "https://dev.to/api/articles"
GH_CONTENTS = "https://api.github.com/repos/{repo}/contents/{path}"
GH_TRAFFIC = "https://api.github.com/repos/{repo}/traffic/popular/paths"

USER_AGENT = "ZeroHumanEngine/1.0 (autonomous content system)"

SEED_TOPICS = [
    "artificial intelligence", "python", "rust", "kubernetes", "cybersecurity",
    "databases", "web development", "linux", "devtools", "startups",
]
STOPWORDS = set(
    "the a an and or but for with from that this these those how why what when "
    "you your our their its it is are was were be been about into over under new "
    "using use uses used get gets got make makes made just now can will would "
    "not no yes vs via per more most best top key big small open free code "
    "show shows guide tutorial review reviews intro part "
    "software app apps tool tools data year years day days week weeks month "
    "first last next real all out up down off other than then them they she he".split()
)

TODAY = dt.date.today().isoformat()
NOW = dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Minimal HTTP layer (stdlib only)
# ---------------------------------------------------------------------------
def http_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_json_auth(url, token):
    return http_json(url, headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json"})


def retry(fn, attempts=3, delay=5):
    for i in range(attempts):
        try:
            return fn()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as ex:
            if i == attempts - 1:
                print(f"[warn] giving up after {attempts} attempts: {ex}")
                return None
            time.sleep(delay * (i + 1))
    return None


# ---------------------------------------------------------------------------
# State management — the "brain" that mutates between runs
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            state = {}
    else:
        state = {}
    if not state.get("topics"):
        state["topics"] = {t: {"weight": 1.0, "views": 0, "stories": 0} for t in SEED_TOPICS}
    if "generation" not in state:
        state["generation"] = 1
    if "posted_stories" not in state:
        state["posted_stories"] = {}  # story_id -> {topic, points_then}
    if "history" not in state:
        state["history"] = []  # [{date, topics: n, top_topic}]
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    RUN_LOG.write_text(json.dumps({
        "last_run": NOW.isoformat(), "generation": state["generation"],
        "topics": len(state["topics"]),
        "top_topic": max(state["topics"], key=lambda t: state["topics"][t]["weight"]),
    }, indent=2))


# ---------------------------------------------------------------------------
# Data acquisition — free public APIs
# ---------------------------------------------------------------------------
def fetch_hn_stories(topic, limit=15):
    params = urllib.parse.urlencode({
        "tags": "story", "query": topic, "hitsPerPage": limit,
        "numericFilters": "points>25",
    })
    data = retry(lambda: http_json(f"{HN_SEARCH}?{params}"))
    if not data or "hits" not in data:
        return []
    out = []
    for h in data["hits"]:
        out.append({
            "id": str(h.get("objectID", "")),
            "title": h.get("title") or "",
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "points": h.get("points") or 0,
            "comments": h.get("num_comments") or 0,
            "author": h.get("author") or "",
            "created": h.get("created_at", "")[:10],
        })
    return [s for s in out if s["title"]]


def fetch_devto_articles(topic, limit=5):
    """Second free, keyless content source: Dev.to public API."""
    params = urllib.parse.urlencode({"tag": topic.split()[0], "top": 7, "per_page": limit})
    data = retry(lambda: http_json(f"{DEVTO_API}?{params}"))
    if not isinstance(data, list):
        return []
    out = []
    for a in data:
        url = a.get("url") or f"https://dev.to{a.get('path', '')}"
        if not a.get("title") or not url:
            continue
        out.append({
            "title": a["title"],
            "url": url,
            "reactions": a.get("positive_reactions_count", 0),
            "comments": a.get("comments_count", 0),
            "author": (a.get("user") or {}).get("username", ""),
            "created": (a.get("published_timestamp") or "")[:10],
        })
    return out


def fetch_github_repos(topic, limit=6):
    since = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    q = f"topic:{topic.split()[0]} stars:>50 created:>{since}"
    params = urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": limit})
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    data = retry(lambda: http_json(f"{GH_SEARCH}?{params}", headers=headers))
    if not data or "items" not in data:
        return []
    return [{
        "name": r["full_name"], "stars": r["stargazers_count"],
        "desc": r.get("description") or "", "url": r["html_url"],
        "lang": r.get("language") or "",
    } for r in data["items"]]


def amazon_search_url(topic):
    q = urllib.parse.urlencode({"k": f"{topic} book", "tag": AMAZON_TAG}) if AMAZON_TAG else ""
    return f"https://www.amazon.com/s?{q}" if q else ""


# ---------------------------------------------------------------------------
# Content selection
# ---------------------------------------------------------------------------
def gather_content(state):
    """For each live topic, pull its best stories + repos. Returns {topic: {...}}"""
    content = {}
    for topic, meta in state["topics"].items():
        stories = fetch_hn_stories(topic)
        articles = fetch_devto_articles(topic)
        repos = fetch_github_repos(topic)
        if not stories and not articles and not repos:
            continue
        # Prefer stories not already published
        fresh = [s for s in stories if s["id"] not in state["posted_stories"]] or stories
        content[topic] = {
            "weight": meta["weight"],
            "stories": sorted(fresh, key=lambda s: s["points"], reverse=True)[:8],
            "articles": sorted(articles, key=lambda a: a["reactions"], reverse=True)[:5],
            "repos": repos,
        }
        time.sleep(1.0)  # be polite to the free APIs
    return content


def extract_keywords(titles):
    words = Counter()
    for t in titles:
        for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", t.lower()):
            if w not in STOPWORDS:
                words[w] += 1
    return [w for w, _ in words.most_common(5)]


# ---------------------------------------------------------------------------
# Site generation — programmatic SEO static site
# ---------------------------------------------------------------------------
PAGE_TPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<link rel="alternate" type="application/rss+xml" href="/rss.xml">
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--card:#161b22;--line:#30363d}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:920px;margin:0 auto;padding:24px}}
header{{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}}
header a{{color:var(--accent);text-decoration:none;font-weight:700;font-size:1.3rem}}
h1{{font-size:1.6rem;margin:16px 0 8px}}
h2{{font-size:1.15rem;margin:28px 0 12px;color:var(--accent)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:12px}}
.card h3{{font-size:1.02rem;margin-bottom:4px}}
.card a{{color:var(--fg);text-decoration:none}}
.card a:hover{{color:var(--accent)}}
.meta{{color:var(--muted);font-size:.85rem}}
.chip{{display:inline-block;background:var(--card);border:1px solid var(--line);color:var(--muted);
border-radius:999px;padding:2px 12px;margin:2px;font-size:.85rem}}
.buy{{display:inline-block;margin-top:8px;background:#1a7f37;color:#fff;padding:6px 14px;border-radius:6px;
text-decoration:none;font-size:.9rem}}
footer{{margin-top:40px;border-top:1px solid var(--line);padding-top:16px;color:var(--muted);font-size:.8rem}}
</style>
</head>
<body>
<div class="wrap">
<header><a href="/">TrendDigest<span style="color:var(--accent)">.io</span></a>
<div class="meta">Autonomous tech intelligence — curated by ZeroHumanEngine · updated {today}</div></header>
{body}
<footer>TrendDigest is generated end-to-end by an autonomous pipeline. Links to Amazon are
affiliate links that may earn this site a commission. <a style="color:var(--muted)" href="/rss.xml">RSS</a></footer>
</div>
</body>
</html>"""


def write_page(path, title, desc, body_html):
    p = SITE_DIR / path
    p.parent.mkdir(parents=True, exist_ok=True)
    if SITE_URL:
        canonical = SITE_URL if path == "index.html" else f"{SITE_URL}/{path}"
    else:
        canonical = "/" + path if path != "index.html" else "/"
    p.write_text(PAGE_TPL.format(
        title=e(title), desc=e(desc)[:155], body=body_html,
        today=TODAY, canonical=e(canonical),
    ))


def story_card(s):
    return (f'<div class="card"><h3><a href="{e(s["url"])}" rel="noopener">{e(s["title"])}</a></h3>'
            f'<div class="meta">{s["points"]} points · {s["comments"]} comments · {e(s["created"])}</div></div>')


def repo_card(r):
    return (f'<div class="card"><h3><a href="{e(r["url"])}" rel="noopener">{e(r["name"])} ★ {r["stars"]}</a></h3>'
            f'<div class="meta">{e(r["desc"][:180])}</div></div>')


def article_card(a):
    return (f'<div class="card"><h3><a href="{e(a["url"])}" rel="noopener">{e(a["title"])}</a></h3>'
            f'<div class="meta">{a["reactions"]} reactions · {a["comments"]} comments · dev.to/{e(a["author"])} · {e(a["created"])}</div></div>')


def jsonld_schema(schema):
    return (f'<script type="application/ld+json">{json.dumps(schema, ensure_ascii=False)}</script>')


def generate_site(content, state):
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    all_topics = sorted(content, key=lambda t: content[t]["weight"], reverse=True)

    # --- Homepage: today's digest across all topics
    body = [f"<h1>Today's Tech Digest — {TODAY}</h1>",
            '<div class="meta">The {n} highest-signal topics right now, ranked by live engagement.</div>'.replace("{n}", str(len(all_topics))),
            '<h2>Live Topics</h2><div>' +
            "".join(f'<a class="chip" href="/t/{urllib.parse.quote(t)}/">{e(t)}</a>' for t in all_topics) +
            "</div>"]
    for topic in all_topics[:5]:
        c = content[topic]
        body.append(f"<h2>{e(topic)}</h2>")
        body.extend(story_card(s) for s in c["stories"][:3])
        if c.get("articles"):
            body.extend(article_card(a) for a in c["articles"][:2])
        if AMAZON_TAG:
            body.append(f'<a class="buy" href="{amazon_search_url(topic)}" rel="nofollow">Shop top {e(topic)} books →</a>')
    write_page("index.html", f"Tech Trend Digest — {TODAY}",
               "Daily autonomous digest of the highest-signal developer and tech stories, ranked by live engagement.",
               "".join(body))

    # --- One SEO landing page per topic (long-tail programmatic SEO)
    for topic, c in content.items():
        slug = urllib.parse.quote(topic)
        body = [f"<h1>{e(topic).title()} — Trending Now</h1>",
                f'<div class="meta">Auto-curated {e(topic)} signal, updated {TODAY}. Generation {state["generation"]}.</div>']
        body.append("<h2>Top Stories</h2>")
        body.extend(story_card(s) for s in c["stories"])
        if c.get("articles"):
            body.append("<h2>From the Community (Dev.to)</h2>")
            body.extend(article_card(a) for a in c["articles"])
        if c["repos"]:
            body.append("<h2>Trending Repositories</h2>")
            body.extend(repo_card(r) for r in c["repos"])
        # JSON-LD structured data: ItemList of curated links for this topic
        items = [{"@type": "ListItem", "position": i + 1,
                  "name": s["title"], "url": s["url"]} for i, s in enumerate(c["stories"][:5])]
        body.append(jsonld_schema({
            "@context": "https://schema.org", "@type": "ItemList",
            "name": f"{topic} — TrendDigest {TODAY}", "itemListElement": items,
        }))
        if AMAZON_TAG:
            body.append(f'<h2>Recommended Reading</h2><a class="buy" href="{amazon_search_url(topic)}" rel="nofollow">'
                        f'Best {e(topic)} books on Amazon →</a>')
        write_page(f"t/{slug}/index.html", f"{topic.title()} — Trending Stories & Repos ({TODAY})",
                   f"Today's highest-engagement {topic} stories, repositories, and reading, auto-curated.",
                   "".join(body))

    # --- Archive page
    hist = state.get("history", [])
    body = ["<h1>Archive</h1>", "".join(f'<div class="card"><div class="meta">{e(h["date"])}</div></div>' for h in hist[-30:])]
    write_page("archive/index.html", "Archive — TrendDigest",
               "Every daily digest ever produced by the pipeline.", "".join(body))

    # --- sitemap.xml / robots.txt / rss.xml / 404
    urls = ["/"] + [f"/t/{urllib.parse.quote(t)}/" for t in content] + ["/archive/"]
    origin = SITE_URL or "https://trenddigest.example"
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
               "".join(f"<url><loc>{origin}/{u.lstrip('/')}</loc>"
                      f"<lastmod>{TODAY}</lastmod></url>" for u in urls) + "</urlset>")
    (SITE_DIR / "sitemap.xml").write_text(sitemap)
    (SITE_DIR / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n")
    items = "".join(
        f'<item><title>{e(s["title"])}</title><link>{e(s["url"])}</link>'
        f'<pubDate>{TODAY}</pubDate></item>'
        for t in all_topics[:5] for s in content[t]["stories"][:3])
    (SITE_DIR / "rss.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f'<title>TrendDigest</title><link>{SITE_URL or "/"}</link><description>Autonomous tech digest</description>'
        f'{items}</channel></rss>')
    write_page("404.html", "Not Found — TrendDigest", "Page not found.",
               "<h1>404</h1><div class='meta'>This page has not been generated (yet).</div>")
    print(f"[site] generated {len(list(SITE_DIR.rglob('*.html')))} pages across {len(content)} topics")


# ---------------------------------------------------------------------------
# AUTONOMOUS SELF-CORRECTION BLOCK
# ---------------------------------------------------------------------------
def self_correct(state, content):
    """
    Reads PUBLIC performance metrics and rewrites the system's own focus:
      (a) GitHub Pages traffic API -> which topic pages humans actually view.
      (b) HN Algolia API -> how stories we published performed after posting.
      (c) Keyword mutation -> mine co-occurring terms, spawn new topics,
          retire dead ones.
    """
    corrections = []

    # (a) Page-view feedback from GitHub repo traffic (free API, same repo)
    if GITHUB_TOKEN and GITHUB_REPO:
        traffic = retry(lambda: http_json_auth(GH_TRAFFIC.format(repo=GITHUB_REPO), GITHUB_TOKEN))
        if traffic and isinstance(traffic, list):
            for entry in traffic:
                path = entry.get("path", "")
                m = re.search(r"/t/([^/]+)/", urllib.parse.unquote(path))
                if m and m.group(1) in state["topics"]:
                    t = m.group(1)
                    views = entry.get("count", 0)
                    state["topics"][t]["views"] += views
                    state["topics"][t]["weight"] *= 1.0 + min(views, 50) / 50.0  # boost up to 2x
                    corrections.append(f"traffic boost +{views} views -> {t}")

    # (b) Story performance: re-check points on stories we published previously
    for sid, meta in list(state["posted_stories"].items())[:15]:
        if meta.get("checked"):
            continue
        # Re-poll the story's current score via Algolia's objectID filter.
        data = retry(lambda s=sid: http_json(f"{HN_SEARCH}?tags=story&filters=objectID:{s}"), attempts=2, delay=2)
        if data and data.get("hits"):
            now_pts = data["hits"][0].get("points", 0)
            delta = now_pts - meta.get("points_then", now_pts)
            if delta > 25:  # strong post-publish growth -> boost its topic
                t = meta.get("topic")
                if t in state["topics"]:
                    state["topics"][t]["weight"] *= 1.15
                    corrections.append(f"story {sid} grew +{delta} pts -> boost {t}")
        meta["checked"] = True
        time.sleep(0.3)

    # (c) Mutation: mine fresh keywords from today's top titles
    top_titles = [s["title"] for c in content.values() for s in c["stories"][:3]]
    existing_words = {w for t in state["topics"] for w in t.split()}
    for kw in extract_keywords(top_titles):
        if kw in existing_words:  # already covered by a live topic
            continue
        if kw not in state["topics"] and len(state["topics"]) < 24:
            state["topics"][kw] = {"weight": 0.4, "views": 0, "stories": 0}  # challenger enters low
            corrections.append(f"new challenger topic: {kw}")

    # (d) Natural selection: decay + retire, normalize
    for t, meta in state["topics"].items():
        meta["weight"] *= 0.97  # everything decays; only winners stay on top
        if meta["weight"] < 0.15 and len(state["topics"]) > len(SEED_TOPICS):
            corrections.append(f"retired topic: {t}")
    state["topics"] = {t: m for t, m in state["topics"].items()
                       if m["weight"] >= 0.15 or t in SEED_TOPICS}
    total = sum(m["weight"] for m in state["topics"].values()) or 1.0
    for m in state["topics"].values():
        m["weight"] = round(max(m["weight"] / total, 0.01) * len(state["topics"]), 3)

    state["generation"] += 1
    state["history"].append({"date": TODAY, "topics": len(state["topics"]),
                             "top_topic": max(state["topics"], key=lambda t: state["topics"][t]["weight"])})
    state["history"] = state["history"][-365:]
    print(f"[self-correct] gen {state['generation']}: " + ("; ".join(corrections) or "no external signal yet"))
    return corrections


def commit_state_back():
    """Persist mutated brain back to the repo via the GitHub Contents API."""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print("[publish] no token; state kept locally (workflow commits it instead)")
        return
    try:
        remote = http_json_auth(GH_CONTENTS.format(repo=GITHUB_REPO, path="state/site_state.json"), GITHUB_TOKEN)
        import base64
        payload = {
            "message": f"autonomous state update {TODAY} gen-{int(time.time())}",
            "content": base64.b64encode(STATE_FILE.read_bytes()).decode(),
            "sha": remote.get("sha", ""),
        }
        req = urllib.request.Request(
            GH_CONTENTS.format(repo=GITHUB_REPO, path="state/site_state.json"),
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json"}, method="PUT")
        urllib.request.urlopen(req, timeout=30)
        print("[publish] state committed via API")
    except Exception as ex:  # noqa: BLE001 — never kill the pipeline on publish failure
        print(f"[warn] state commit failed (non-fatal): {ex}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    print(f"[boot] ZeroHumanEngine run {NOW.isoformat()} repo={GITHUB_REPO or 'local'}")
    state = load_state()
    content = gather_content(state)
    if not content:
        print("[error] no content fetched from any API; aborting run")
        sys.exit(1)
    for t, c in content.items():
        for s in c["stories"]:
            state["posted_stories"][s["id"]] = {"topic": t, "points_then": s["points"]}
    state["posted_stories"] = dict(list(state["posted_stories"].items())[-2000:])
    generate_site(content, state)
    self_correct(state, content)
    save_state(state)
    commit_state_back()
    print("[done] run complete; site in ./site")


if __name__ == "__main__":
    main()
