#!/usr/bin/env python3
"""
Personal job board scraper.

Reads config.yaml, visits each site, keeps the links that match your keywords,
and merges them into docs/jobs.json, which docs/index.html displays.

    python scraper.py                      # normal run
    python scraper.py --config other.yaml  # use another config
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent

UA_TOKEN = "PersonalJobBoard"
UA = f"Mozilla/5.0 (compatible; {UA_TOKEN}/1.0)"
TIMEOUT = 25            # seconds per request
DELAY = 1.0             # pause between sites, to stay polite
MAX_PER_SITE = 150      # safety cap on matches from one page
DROP_AFTER_DAYS = 30    # forget postings that have been gone this long

# Link texts that say nothing about the job; we take the surrounding text instead.
GENERIC_TEXT = {
    "read more", "en savoir plus", "voir l offre", "voir l'offre", "apply",
    "postuler", "learn more", "details", "lire la suite", "view", "voir",
    "view job", "see more", "plus d infos", "more", "more info",
}


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def norm(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\u2019\u2018\u02bc`'\-\u2013\u2014]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def compile_keyword(keyword: str) -> re.Pattern:
    k = norm(keyword)
    if k.endswith("*"):
        return re.compile(r"(?<!\w)" + re.escape(k[:-1]) + r"\w*")
    # allow plural and feminine endings: analyste/analystes, charge/chargee
    return re.compile(r"(?<!\w)" + re.escape(k) + r"(?:e|s|es)?(?!\w)")


def job_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed_by_robots(session: requests.Session, url: str) -> bool:
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=10)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                _robots_cache[base] = rp
            else:
                _robots_cache[base] = None   # no robots.txt: allowed
        except requests.RequestException:
            _robots_cache[base] = None
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(UA_TOKEN, url)


def fetch_static(session: requests.Session, url: str) -> tuple[str, str]:
    last: Exception | None = None
    for _ in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            if r.encoding in (None, "ISO-8859-1"):
                r.encoding = r.apparent_encoding
            return r.text, r.url
        except requests.RequestException as exc:
            last = exc
            time.sleep(2)
    raise last  # type: ignore[misc]


class Browser:
    """Headless Chromium, started only if a site asks for render: js."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def html(self, url: str) -> tuple[str, str]:
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # may raise ImportError
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        context = self._browser.new_context(user_agent=UA)
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            return page.content(), page.url
        finally:
            context.close()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# --------------------------------------------------------------------------
# Extracting postings from a page
# --------------------------------------------------------------------------
def extract(html: str, base_url: str, site: dict, matchers, exclude) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    page_text_len = len(clean(soup.get_text(" ")))

    take_all = bool(site.get("take_all"))
    use_context = bool(site.get("match_context"))
    found: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if urlparse(url).scheme not in ("http", "https"):
            continue

        text = clean(a.get_text(" ")) or clean(a.get("title", ""))
        container = a.find_parent(["li", "article", "tr", "div"])
        context = clean(container.get_text(" "))[:240] if container else ""
        if norm(re.sub(r"[^\w' ]", " ", text)) in GENERIC_TEXT or len(text) < 4:
            text = clean(context.replace(text, "", 1))[:140] or text
        if len(text) < 4:
            continue

        slug = unquote(urlparse(url).path).replace("-", " ").replace("_", " ")
        haystack = norm(" ".join([text, slug, context if use_context else ""]))
        if any(p.search(haystack) for p in exclude):
            continue

        matched = [kw for kw, pat in matchers if pat.search(haystack)]
        if not matched and not take_all:
            continue
        if take_all and not matched:
            # Keep only links that look like individual postings, not menus.
            if len(text) < 12 or urlparse(url).path in ("", "/"):
                continue

        title = text if len(text) <= 180 else text[:177] + "..."
        previous = found.get(url)
        if previous is None or len(title) > len(previous["title"]):
            found[url] = {"title": title, "url": url, "matched": matched}
        if len(found) >= MAX_PER_SITE:
            break

    return list(found.values()), page_text_len


# --------------------------------------------------------------------------
# One site
# --------------------------------------------------------------------------
def check_site(site, session, browser, matchers, exclude):
    status = {"name": site["name"], "url": site["url"], "status": "ok",
              "matches": 0, "message": ""}
    results: list[dict] = []
    try:
        if not site.get("ignore_robots") and not allowed_by_robots(session, site["url"]):
            status.update(status="blocked", message="robots.txt does not allow automated access")
            return status, results

        if site.get("render") == "js":
            html, final_url = browser.html(site["url"])
        else:
            html, final_url = fetch_static(session, site["url"])

        results, text_len = extract(html, final_url, site, matchers, exclude)
        for r in results:
            r["source"] = site["name"]
            r["source_url"] = site["url"]
        status["matches"] = len(results)
        if text_len < 300 and not results:
            hint = "" if site.get("render") == "js" else " Try adding render: js for this site."
            status.update(status="empty", message="The page came back almost empty." + hint)
    except ImportError:
        status.update(status="error", message="Playwright is not installed (needed for render: js)")
    except Exception as exc:  # noqa: BLE001 - we want to keep going whatever happens
        status.update(status="error", message=f"{type(exc).__name__}: {str(exc)[:160]}")
    return status, results


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def merge(state: dict, results: list[dict], statuses: list[dict], now: datetime) -> None:
    jobs = {j["id"]: j for j in state.get("jobs", [])}
    ok_urls = {s["url"] for s in statuses if s["status"] in ("ok", "empty")}
    seen: set[str] = set()
    stamp = iso(now)

    for r in results:
        jid = job_id(r["url"])
        seen.add(jid)
        if jid in jobs:
            j = jobs[jid]
            j.update(title=r["title"], matched=r["matched"], last_seen=stamp, active=True,
                     source=r["source"], source_url=r["source_url"])
        else:
            jobs[jid] = {"id": jid, "title": r["title"], "url": r["url"],
                         "source": r["source"], "source_url": r["source_url"],
                         "matched": r["matched"], "first_seen": stamp,
                         "last_seen": stamp, "active": True}

    # A posting that disappeared from a page we read successfully is closed.
    # If the page failed this time, we say nothing about its postings.
    for jid, j in jobs.items():
        if jid not in seen and j["source_url"] in ok_urls:
            j["active"] = False

    cutoff = iso(now - timedelta(days=DROP_AFTER_DAYS))
    state["jobs"] = [j for j in jobs.values() if j["active"] or j["last_seen"] >= cutoff]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "jobs.json"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    keywords = cfg.get("keywords") or []
    sites = cfg.get("sites") or []
    if not sites:
        print("config.yaml has no sites.", file=sys.stderr)
        return 2
    matchers = [(k, compile_keyword(k)) for k in keywords]
    exclude = [compile_keyword(x) for x in cfg.get("exclude") or []]

    out = Path(args.out)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    state = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    state.setdefault("created", iso(now))
    state.setdefault("jobs", [])

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en,fr;q=0.8"})
    browser = Browser()

    statuses: list[dict] = []
    results: list[dict] = []
    try:
        for i, site in enumerate(sites, 1):
            status, found = check_site(site, session, browser, matchers, exclude)
            statuses.append(status)
            results.extend(found)
            print(f"[{i:>3}/{len(sites)}] {status['status']:<8} {status['matches']:>3}  {site['name']}"
                  + (f"  ({status['message']})" if status["message"] else ""))
            time.sleep(DELAY)
    finally:
        browser.close()

    if not any(s["status"] in ("ok", "empty") for s in statuses):
        print("Every site failed. Keeping the previous data (is the network down?).", file=sys.stderr)
        return 1

    merge(state, results, statuses, now)
    state["updated"] = iso(now)
    state["keywords"] = keywords
    state["sources"] = statuses

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    active = sum(1 for j in state["jobs"] if j["active"])
    ok = sum(1 for s in statuses if s["status"] == "ok")
    print(f"\nDone: {active} active postings, {ok}/{len(statuses)} sites read without problems.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
