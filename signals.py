"""Daily Automotive Sales Signals.

Collects public automotive news, removes noise and duplicates, tags each story (AI if
ANTHROPIC_API_KEY is set, keyword rules otherwise), sorts it into sections, and emails one
report to everyone in config.yaml at the configured local time.

    python signals.py              # scheduled mode: sends only if it's the send hour and not sent today
    python signals.py --preview    # build the email now, write it to out/, send nothing
    python signals.py --send-now   # build and send now, ignoring the clock
    python signals.py --send-now --to me@example.com   # send a test copy to one address

Email settings come from environment variables: SMTP_USER, SMTP_PASS (a Gmail app password),
and optionally SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465), MAIL_FROM.
Optional: EXA_API_KEY adds the Exa news searches listed in rules.yaml as extra sources.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
USER_AGENT = "auto-sales-signals/1.0"

# The email has four sections, in this order: market-wide developments (Industry trends), news about dealers,
# groups and carmakers the team could sell to (Sales signals), news about competing dealer software providers
# (Competitors), and news about us (Tekion). Each item is labelled company · region.
SECTIONS = ["industry_trends", "account_news", "competitors", "tekion"]
SECTION_TITLES = {"industry_trends": "Industry trends", "account_news": "Sales signals",
                  "competitors": "Competitors", "tekion": "Tekion"}
# Regions a team can cover (config.yaml `regions`). The AI tags each story with one of these, or Global / Unknown.
REGIONS = ["North America", "Europe", "India", "Middle East", "Asia Pacific", "Latin America", "Africa"]
STORY_REGIONS = REGIONS + ["Global", "Unknown"]
REGION_HINTS = {"North America": "the United States or Canada", "Europe": "Europe or the UK", "India": "India",
                "Middle East": "the Middle East", "Asia Pacific": "Asia Pacific", "Latin America": "Latin America", "Africa": "Africa"}
SIGNALS = [
    "acquisition", "closure", "new_store", "market_entry", "competitor_selected", "leadership_change", "tech_investment",
    "oem_dms_certification", "oem_retail_strategy", "major_recall", "sales_shift", "launch",
    "competitor_incident", "regulation", "vendor_news", "market_trend",
]
BRAND_SIGNALS = {"oem_dms_certification", "oem_retail_strategy", "major_recall", "sales_shift", "launch"}
ROLES = ["acquirer", "target", "subject", "certified", "other"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- config

class ConfigError(Exception):
    pass


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_config(path: Path) -> dict:
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"cannot read {path.name}: {e}")
    recipients = cfg.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",")]
    bad = [r for r in recipients if not EMAIL_RE.match(str(r))]
    if not recipients or bad:
        raise ConfigError(f"{path.name}: recipients must be a list of email addresses" + (f" (invalid: {', '.join(map(str, bad))})" if bad else ""))
    hour = cfg.get("send_hour", 8)
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ConfigError(f"{path.name}: send_hour must be a whole number from 0 to 23")
    tz = cfg.get("timezone", "UTC")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{path.name}: unknown timezone {tz!r} (use names like America/Chicago or Asia/Kolkata)")
    regions = cfg.get("regions") or []
    if isinstance(regions, str):
        regions = [regions]
    by_lower = {r.lower(): r for r in REGIONS}
    unknown = [r for r in regions if str(r).strip().lower() not in by_lower]
    if unknown:
        raise ConfigError(f"{path.name}: unknown region(s) {', '.join(map(str, unknown))} (use: {', '.join(REGIONS)})")
    contact = str(cfg.get("contact") or "").strip()
    return {"recipients": [str(r) for r in recipients], "send_hour": hour, "timezone": tz,
            "regions": [by_lower[str(r).strip().lower()] for r in regions], "contact": contact}


def load_rules(path: Path) -> dict:
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))

    def compile_all(patterns, where):
        out = []
        for p in patterns or []:
            try:
                out.append(re.compile(p, re.I))
            except re.error as e:
                raise ConfigError(f"{path.name}: bad pattern in {where}: {p!r} ({e})")
        return out

    pf = rules["prefilter"]
    rules["_brand_exclude"] = compile_all(rules.get("brand_exclude"), "brand_exclude")
    rules["_exclude"] = compile_all(pf.get("exclude"), "prefilter.exclude")
    rules["_exclude_url"] = compile_all(pf.get("exclude_url"), "prefilter.exclude_url")
    rules["_include"] = compile_all(pf.get("include"), "prefilter.include")
    rules["_signals"] = []
    for name, spec in rules["signals"].items():
        if name not in SIGNALS:
            raise ConfigError(f"{path.name}: unknown signal {name!r}")
        rules["_signals"].append({
            "signal": name,
            "patterns": compile_all(spec["patterns"], f"signals.{name}"),
            "implication": spec.get("implication", ""),
            "rising": re.compile(spec["rising"], re.I) if spec.get("rising") else None,
        })
    return rules


# ---------------------------------------------------------------- text helpers

TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|ref|cmpid|mc_.*|ncid|cmp|src|ocid|yclid|_ga|igshid)$", re.I)


def canonical_url(raw: str) -> str:
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return raw.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)))
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunsplit(("https", host, path, query, ""))


def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(html.unescape(s))).strip()


def strip_publisher_suffix(title: str, publisher: str | None) -> str:
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3].strip()
    m = re.match(r"^(.*\S)\s+[-|–]\s+[^-|–]{2,60}$", title)
    return m.group(1) if m else title


NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
                "eleven": "11", "twelve": "12", "twenty": "20", "hundred": "100", "thousand": "1000", "first": "1", "second": "2", "third": "3"}
STOP = set(("a an the and or but of to in on at for with by from as is are was were be been it its this that these those into over after before "
            "about new says said say report reports reported news update latest today announces announced announce amid via has have had will "
            "than more up out per s inc ltd llc co corp plc group").split())


def stem(t: str) -> str:
    if len(t) <= 4 or any(c.isdigit() for c in t):
        return t
    return re.sub(r"e$", "", re.sub(r"(ings?|ed|es|s)$", "", t)) or t


def title_tokens(title: str) -> list[str]:
    t = re.sub(r"['’]s\b", "", title.lower())
    t = re.sub(r"[^\w\s]|_", " ", t)
    out = []
    for w in t.split():
        w = NUMBER_WORDS.get(w, w)
        if w in STOP:
            continue
        w = stem(w)
        if w not in out:
            out.append(w)
    return out


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 0.0


SUFFIXES = {"inc", "incorporated", "ltd", "limited", "llc", "plc", "corp", "corporation", "co", "company", "group", "automotive",
            "holdings", "the", "pvt", "private", "sa", "ag", "gmbh", "na", "usa"}


def norm_company(name: str) -> str:
    tokens = re.sub(r"[^\w\s]|_", " ", name.lower().replace("&", " and ")).split()
    while len(tokens) > 1 and tokens[-1] in SUFFIXES:
        tokens.pop()
    while len(tokens) > 1 and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def word_re(s: str):
    return re.compile(rf"(?<![^\W_]){re.escape(s)}(?![^\W_])", re.I)


# ---------------------------------------------------------------- collect

def fetch(url: str, settings: dict) -> bytes:
    last = None
    for attempt in range(settings["retries"] + 1):
        try:
            r = requests.get(url, timeout=settings["timeout_seconds"], headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            last = e
            if attempt < settings["retries"]:
                time.sleep(0.3 * 2 ** attempt)
    raise last  # type: ignore[misc]


def parse_feed(body: bytes, source: dict) -> list[dict]:
    feed = feedparser.parse(body)
    if feed.bozo and not feed.entries:
        raise ValueError("not an RSS or Atom feed")
    out = []
    for e in feed.entries:
        published = e.get("published_parsed") or e.get("updated_parsed")
        if not published:
            continue
        publisher = source["name"]
        if source["type"] == "google_news" and e.get("source"):
            publisher = e.source.get("title") or publisher
        out.append({
            "source_id": source["id"],
            "url": e.get("link", ""),
            "title": e.get("title", ""),
            "snippet": e.get("summary", ""),
            "published": dt.datetime(*published[:6], tzinfo=dt.timezone.utc),
            "publisher": publisher,
        })
    return out


def parse_nhtsa(body: bytes, source: dict) -> list[dict]:
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise ValueError("unexpected NHTSA response")
    out = []
    for r in rows:
        n = int(r.get("potentially_affected") or 0)
        if n < source.get("min_affected", 0) or not r.get("nhtsa_id") or not r.get("manufacturer"):
            continue
        out.append({
            "source_id": source["id"],
            "url": (r.get("recall_link") or {}).get("url") or f"https://www.nhtsa.gov/recalls?nhtsaId={r['nhtsa_id']}",
            "title": f"{r['manufacturer']} recalls {n:,} vehicles: {r.get('subject') or r.get('component') or 'safety defect'}",
            "snippet": (r.get("defect_summary") or "")[:600],
            "published": dt.datetime.fromisoformat(r["report_received_date"][:19]).replace(tzinfo=dt.timezone.utc),
            "publisher": "NHTSA",
        })
    return out


EXA_URL = "https://api.exa.ai/search"


def exa_search(source: dict, since: dt.datetime, settings: dict, key: str) -> dict:
    """One Exa news search: articles published since the last email, with a few sentences from each."""
    payload = {
        "query": source["query"],
        "type": "auto",
        "category": "news",
        "numResults": source.get("num_results", 10),
        "startPublishedDate": since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "contents": {"highlights": {"numSentences": 3, "highlightsPerUrl": 1}},
    }
    if source.get("exclude_domains"):
        payload["excludeDomains"] = source["exclude_domains"]
    last = None
    for attempt in range(settings["retries"] + 1):
        try:
            r = requests.post(EXA_URL, json=payload, timeout=max(settings["timeout_seconds"], 20),
                              headers={"x-api-key": key, "User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            if attempt < settings["retries"]:
                time.sleep(0.3 * 2 ** attempt)
    raise last  # type: ignore[misc]


URL_DATE = re.compile(r"/(20\d{2})[/-](\d{1,2})(?:[/-](\d{1,2}))?(?=[/-])")


def url_date_is_older(url: str, published: dt.datetime, slack_days: int = 7) -> bool:
    """True when the URL carries a date (/2023/05/ or /2023-05-14-) well before the reported publish date."""
    m = URL_DATE.search(urlsplit(url).path + "/")
    if not m:
        return False
    try:
        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 28))
    except ValueError:
        return False
    return d < published.date() - dt.timedelta(days=slack_days)


def parse_exa(data: dict, source: dict) -> list[dict]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("unexpected Exa response")
    out = []
    for r in results:
        url, title, published = r.get("url"), (r.get("title") or "").strip(), r.get("publishedDate")
        if not url or not title or not published:
            continue
        try:
            when = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if url_date_is_older(url, when):
            continue  # Exa sometimes reports a recent date for an old page; the URL says otherwise
        host = urlsplit(url).hostname or source["name"]
        snippet = " ".join(r.get("highlights") or []) or r.get("summary") or (r.get("text") or "")[:600]
        out.append({
            "source_id": source["id"],
            "url": url,
            "title": title,
            "snippet": snippet,
            "published": when.astimezone(dt.timezone.utc),
            "publisher": host.removeprefix("www."),
        })
    return out


def collect(rules: dict, since: dt.datetime, now: dt.datetime, fetcher=None, exa=None) -> tuple[list[dict], list[str], list[dict]]:
    """Fetch every source in parallel. Returns (items in window, ok source ids, failed [{id, error}]).
    Exa searches run only when EXA_API_KEY is set (or an `exa` function is passed in, for tests)."""
    settings = rules["settings"]["fetch"]
    fetcher = fetcher or (lambda url: fetch(url, settings))
    if exa is None and os.environ.get("EXA_API_KEY"):
        key = os.environ["EXA_API_KEY"]
        exa = lambda source, start: exa_search(source, start, settings, key)  # noqa: E731
    sources = [s for s in rules["sources"] if s["type"] != "exa" or exa]
    ok, failed, items = [], [], []

    def one(source):
        if source["type"] == "exa":
            return parse_exa(exa(source, since), source)
        url = source["url"]
        if source["type"] == "nhtsa":
            url += "?" + urlencode({"$limit": 200, "$order": "report_received_date DESC", "$where": f"report_received_date >= '{since:%Y-%m-%d}T00:00:00'"})
        body = fetcher(url)
        return parse_nhtsa(body, source) if source["type"] == "nhtsa" else parse_feed(body, source)

    with concurrent.futures.ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
        futures = {pool.submit(one, s): s for s in sources}
        for fut in concurrent.futures.as_completed(futures):
            source = futures[fut]
            try:
                raw = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad source never stops the run
                failed.append({"id": source["id"], "error": str(e)[:200]})
                continue
            ok.append(source["id"])
            for it in raw:
                if since <= it["published"] <= now + dt.timedelta(hours=1):
                    it["tier"] = source["tier"]
                    it["auto_only"] = source.get("auto_only", False)
                    it["is_aggregator"] = source["type"] == "google_news"
                    it["from_search"] = source["type"] in ("google_news", "exa")
                    items.append(it)
    return items, ok, failed


# ---------------------------------------------------------------- clean: normalise, pre-filter, dedupe

def rate_outlets(items: list[dict], rules: dict) -> list[dict]:
    """Search results (Google News, Exa) come from any site. One from a trusted outlet (rules.yaml
    trusted_outlets, by domain or name) gets tier 1, so it wins when outlets carry the same story and
    sorts ahead of unknown sites within a section."""
    domains, names = set(), set()
    for entry in rules.get("trusted_outlets", []):
        d, _, n = str(entry).partition("|")
        if d.strip():
            domains.add(d.strip().lower())
        if n.strip():
            names.add(n.strip().lower())
    for it in items:
        if not it.get("from_search"):
            continue
        host = (urlsplit(it["url"]).hostname or "").lower().removeprefix("www.")
        pub = str(it.get("publisher", "")).strip().lower().removeprefix("www.")
        if any(host == d or host.endswith("." + d) or pub == d for d in domains) or pub in names:
            it["tier"] = 1
    return items


def normalize(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if not it["title"] or not it["url"]:
            continue
        title = strip_html(strip_publisher_suffix(it["title"], it["publisher"]) if it["is_aggregator"] else it["title"])
        if not title:
            continue
        # Google News snippets only repeat the headline.
        snippet = "" if it["is_aggregator"] else strip_html(it["snippet"])
        if len(snippet) > 500:
            snippet = re.sub(r"\s+\S*$", "", snippet[:497]) + "..."
        out.append({**it, "title": title, "snippet": snippet, "canonical": canonical_url(it["url"]), "tokens": title_tokens(title)})
    return out


def without_brand_lookalikes(text: str, rules: dict) -> str:
    """Blank out names like "HD Hyundai" that contain a car brand but aren't the car business."""
    for rx in rules.get("_brand_exclude", []):
        text = rx.sub(" ", text)
    return text


def prefilter(items: list[dict], rules: dict) -> tuple[list[dict], int]:
    out, dropped = [], 0
    for it in items:
        # Exclusions check the title and URL only: snippets mention "review" or "deals" in passing.
        if any(r.search(it["title"]) for r in rules["_exclude"]) or any(r.search(it["url"]) for r in rules["_exclude_url"]):
            dropped += 1
            continue
        if not it["auto_only"] and not any(r.search(without_brand_lookalikes(f"{it['title']} {it['snippet']}", rules)) for r in rules["_include"]):
            dropped += 1
            continue
        out.append(it)
    return out, dropped


def dedupe(items: list[dict], similarity: float, window_hours: int = 48) -> list[dict]:
    """Same link, or the same story from another outlet → one story. The best source becomes the primary:
    lowest tier (press release first), then earliest. Returns primaries with `outlets` and `urls`."""
    ordered = sorted(items, key=lambda i: (i["tier"], i["published"]))
    stories: list[dict] = []
    seen_urls: set[str] = set()
    for it in ordered:
        if it["canonical"] in seen_urls:
            continue
        seen_urls.add(it["canonical"])
        match = None
        for s in stories:
            if abs((s["published"] - it["published"]).total_seconds()) <= window_hours * 3600 and jaccard(s["tokens"], it["tokens"]) >= similarity:
                match = s
                break
        if match:
            match["publishers"].add(it["publisher"])
            match["urls"].add(it["canonical"])
        else:
            stories.append({**it, "publishers": {it["publisher"]}, "urls": {it["canonical"]}})
    for s in stories:
        s["outlets"] = len(s["publishers"])
    return stories


def drop_already_sent(stories: list[dict], sent: list[dict], similarity: float) -> list[dict]:
    sent_urls = {x["url"] for x in sent}
    out = []
    for s in stories:
        if s["urls"] & sent_urls or any(jaccard(s["tokens"], x["tokens"]) >= similarity for x in sent):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- classify: keyword rules

NOT_COMPANY = set(("The A An In On At For With By From To Of And Or But After Before New How Why What When Where Who Which This That These Those Its It Is "
                   "Are Was Were Has Have Will Report Reports Exclusive Update Breaking Watch Video Photos Sales Recall Recalls Launch Launches Dealers "
                   "Dealer Dealership Dealerships EV EVs SUV SUVs US UK EU India China Japan Korea Middle East North America Europe CEO CIO CTO CFO COO "
                   "President Chief Q1 Q2 Q3 Q4 January February March April May June July August September October November December Monday Tuesday "
                   "Wednesday Thursday Friday Saturday Sunday").split())
COMPANY_WORD = re.compile(r"\b(group|motors|automotive|auto|autos|dealerships?|holdings|inc|ltd|llc|corp|cars|motor company)\b", re.I)
ACQ_VERB = re.compile(r"\b(acquires?|acquired|acquiring|to acquire|buys|bought|to buy|purchases?|purchased|agrees to buy|agrees to acquire|completes acquisition of|takes over)\b", re.I)
PASSIVE_ACQ = re.compile(r"\b(acquired by|bought by|sold to|sells (?:\w+ )*to|to be acquired by)\b", re.I)
CAP_PHRASE = re.compile(r"\b([A-Z][^\W_]*[\w&'.-]*(?:\s+(?:[A-Z][\w&'.-]+|of|and|&))*)")


def capitalised_phrases(title: str) -> list[str]:
    out = []
    for m in CAP_PHRASE.finditer(title):
        words = [w for w in m.group(1).split() if re.sub(r"[^\w]", "", w) not in NOT_COMPANY]
        while words and words[-1] in ("of", "and", "&"):
            words.pop()
        phrase = " ".join(words).strip()
        if len(phrase) >= 3 and (len(words) >= 2 or re.search(r"group|motors|automotive|auto\b", phrase, re.I)):
            out.append(phrase)
    return out


class RulesContext:
    def __init__(self, rules: dict):
        self.rules = rules
        known = []
        for c in [rules["us"]] + rules["competitors"]:
            kind = "us" if c is rules["us"] else "competitor"
            for n in [c["name"]] + c.get("aliases", []):
                known.append((c["name"], word_re(n), kind))
        known.sort(key=lambda k: -len(k[1].pattern))
        self.known = known
        names = list(dict.fromkeys(rules["brands"]))
        self.brands = sorted(((n, word_re(n)) for n in names), key=lambda b: -len(b[0]))


def classify_rules(story: dict, ctx: RulesContext) -> dict:
    title, text = story["title"], f"{story['title']} {story['snippet']}"
    signal, in_title, implication, rising = None, False, "", None
    for target, flag in ((title, True), (text, False)):
        for s in ctx.rules["_signals"]:
            if any(p.search(target) for p in s["patterns"]):
                signal, in_title, rising = s["signal"], flag, s["rising"]  # no stock "why it matters" line
                break
        if signal:
            break

    found, seen = [], set()
    clean_text = without_brand_lookalikes(text, ctx.rules)
    for name, rx, kind in ctx.known:
        key = norm_company(name)
        m = rx.search(clean_text)
        if m and key not in seen:
            seen.add(key)
            tm = rx.search(without_brand_lookalikes(title, ctx.rules))
            found.append({"name": name, "idx": tm.start() if tm else 10000 + m.start(), "known": True, "kind": kind})
    clean_title = without_brand_lookalikes(title, ctx.rules)
    brand_hits = [n for n, rx in ctx.brands if rx.search(clean_title)]  # brands from the title only
    brand_keys = {norm_company(b) for b in brand_hits}
    for p in capitalised_phrases(title):
        n = norm_company(p)
        if n in seen or n in brand_keys or any(n in s or s in n for s in seen):
            continue
        seen.add(n)
        found.append({"name": p, "idx": title.find(p), "known": False, "kind": "named" if COMPANY_WORD.search(p) else None})
    found.sort(key=lambda f: f["idx"])
    companies = [{"name": f["name"], "role": "subject"} for f in found[:4]]

    if signal == "acquisition":
        verb = PASSIVE_ACQ.search(title) or ACQ_VERB.search(title)
        passive = bool(PASSIVE_ACQ.search(title))
        if verb:
            for c, f in zip(companies, found):
                if f["idx"] >= 10000:
                    continue
                before = f["idx"] < verb.start()
                c["role"] = ("target" if before else "acquirer") if passive else ("acquirer" if before else "target")
    if signal == "oem_dms_certification":
        for c, f in zip(companies, found):
            if f["kind"] in ("us", "competitor"):
                c["role"] = "certified"

    named_in_title = any((f["known"] or f["kind"] == "named") and f["idx"] < 10000 for f in found)
    brand_in_title = signal in BRAND_SIGNALS and bool(brand_hits)
    confidence = 0.75 if signal and in_title and (named_in_title or brand_in_title) else 0.5
    direction = None
    if signal == "sales_shift":
        direction = "rising" if rising and rising.search(title) else "falling"
    summary = re.sub(r"\s+", " ", title).strip()
    if summary and summary[-1] not in ".!?":
        summary += "."
    return {"relevant": signal is not None, "signal": signal, "companies": companies, "brands": brand_hits[:5], "direction": direction,
            "summary": summary, "implication": implication, "confidence": confidence, "region": "Unknown", "outlet": "", "buyer": None, "tagged_by": "rules"}


# ---------------------------------------------------------------- classify: AI (optional)

SIGNAL_DEFS = """Company signals (about a specific dealer group, dealer, distributor or OEM):
- acquisition: a company buys, merges with, or is bought by another (dealerships, stores, groups), including a buy-sell, private equity investment in a dealer group, or a family ownership transition. Give roles: acquirer and target.
- closure: a store/dealership closes, a franchise is lost or terminated, a group goes bankrupt, or a company announces a restructuring, layoffs or cost cuts.
- new_store: a new dealership, showroom or store opens or breaks ground.
- market_entry: a brand or company enters a new country/market, or appoints a distributor/importer.
- competitor_selected: a dealer or group selects, signs with, or rolls out a competing dealer software provider (listed below).
- leadership_change: a senior executive (CEO, CIO, CTO, CFO, COO, president, dealer principal, controller, fixed operations director, head of digital/IT) is appointed or leaves.
- tech_investment: a dealer, group or OEM invests in retail technology, digital retail, AI, software or IT modernisation.
Brand signals (about a vehicle brand / OEM, affecting its dealers):
- oem_dms_certification: an OEM certifies or approves a dealer management system (DMS) vendor for its dealers. Mark the certified vendor with role "certified".
- oem_retail_strategy: an OEM changes how its vehicles are sold (agency model, direct sales, dealer network changes, dealer standards). Set direction: "falling" if the change reduces the role or number of dealers (direct sales, agency model, network cuts), "rising" if it expands it.
- major_recall: a recall of 100,000 or more vehicles. Smaller recalls are relevant: false.
- sales_shift: a notable rise or fall in vehicle sales, registrations, market share or EV share, for a market or a single brand (including a brand's monthly sales results). Set direction.
- launch: a new model, EV or brand launch, or a launch date announced. Concept cars and car reviews are relevant: false.
Market signals:
- competitor_incident: an outage, cyberattack, breach, lawsuit or regulatory action involving a competing dealer software provider.
- regulation: a new, proposed or court-ordered law, regulation, ruling or tariff affecting auto dealers or vehicle sales, or trade and tariff negotiations that name the auto sector (say in the summary that it is proposed or under negotiation). General political statements that are not about autos are relevant: false.
- market_trend: industry-wide data or a study about auto retail: inventory levels, sales pace, used-vehicle prices, loan rates, dealer profitability, dealership deal volume or valuations, or a survey of how dealers use technology (AI, digital retail, cloud, cybersecurity). Not about one company.
- vendor_news: other news about our company or a competing dealer software provider: a dealer or OEM choosing our company, product launches, partnerships, funding, acquisitions, pricing, layoffs, or a C-suite change or a new head of sales, product or engineering. Routine hires below the C-suite outside sales and product (HR, finance, marketing, regional roles) are relevant: false. Name the provider as a company with role "subject"."""


def system_prompt(rules: dict) -> str:
    us = ", ".join([rules["us"]["name"]] + rules["us"].get("aliases", []))
    comps = "; ".join(" / ".join([c["name"]] + c.get("aliases", [])) for c in rules["competitors"])
    products = "; ".join(rules["us"].get("products", [])) or "our dealer and OEM software"
    return f"""You tag automotive news items for a business development team that sells dealer management and automotive retail software to carmakers, distributors and dealer groups.

Our company: {us}. Our products: {products}.
Competing dealer software providers: {comps}.

For each item, decide whether it is relevant to B2B automotive retail (dealers, dealer groups, distributors, OEM retail and sales, dealer software). Consumer content (car reviews, buying advice, motorsport, deals, celebrity, general business news unrelated to auto retail) is relevant: false, with signal null. This is about car and light-vehicle retail (commercial trucks count): motorcycle and other two-wheeler dealers, and construction, agricultural or heavy-equipment dealers (Caterpillar, John Deere), are relevant: false.

Signals (choose exactly one for relevant items, the best fit):
{SIGNAL_DEFS}

Company roles: acquirer, target (the one being bought or getting new owners), certified, subject (the company the news is about), other (named, but the news is not about it, e.g. mentioned as background or history).

Rules:
- Extract only companies and brands that are named in the title or snippet. Use the name as written. Never infer or add names.
- companies are businesses (dealer groups, dealers, OEMs as corporate actors, vendors). brands are vehicle brands. An OEM can appear in both.
- summary: the fact only, in one plain sentence of at most 35 words. No opinion, no implication.
- implication: one sentence of at most 30 words.
  - Sales signals (buyer is true and the signal is acquisition, closure, new_store, market_entry, competitor_selected, leadership_change or tech_investment): always write it, and say which of our products fits this event and why, naming the specific product or module from the list above. Examples: a new store needs its DMS, CRM and payments set up (Automotive Retail Cloud: DMS, CRM, Tekion Pay); a group buying stores moves them onto one system (Automotive Retail Cloud, Advanced Analytics); a carmaker building a dealer network or changing how it sells (Automotive Enterprise Cloud); a new head of digital or service (Digital Retail, Digital Service Experience); a dealer choosing a competitor (the Tekion modules that compete with what they chose).
  - Everything else: write it whenever you can say something specific about what this event means for the companies involved, and leave it empty when the line would be generic. Name a product only when the link to this event is direct. Make it concrete to this story (name the company or the change); no generic line that would fit any story of this type, and never the same sentence for two items. Do not hedge with "can", "may", "might", "could", "typically" or "potentially". Never say when or how to act. Never use the words "should", "reach out", "contact", "opportunity to". Formal, plain language.
- confidence: 0 to 1, how clearly this is hot news: a specific, new event or fresh data point from the past few days, stated as fact, with the signal, companies and roles clear from the text. Use below 0.5 for general knowledge, explainers, how-to or advice pieces, opinion, predictions, evergreen features, or a rehash of known facts, and when the text is too thin to tell what happened.
- direction: "rising" or "falling" for sales_shift and oem_retail_strategy, otherwise null.
- region: where the event happens: one of North America, Europe (including the UK), India, Middle East, Asia Pacific (including China, Japan, Korea, Southeast Asia, Australia), Latin America, Africa; "Global" if it spans regions; "Unknown" if the text doesn't say.
- english: false if the title or snippet is not in English.
- current: false if the item is not news from the past few days relative to "today" (an old article, an anniversary piece, an evergreen feature or cover story, or a recap of past events), or if the event itself happened more than 7 days before "today" even though the article is new (for example "launched on 1 October" in a story published in mid-November). The publishedAt date can be wrong; judge from the text too.
- buyer: true if the main company the news is about is a business that would buy dealer software: a dealer, dealer group, distributor or importer, or a carmaker's sales and retail side. false for insurers, lenders, warranty or F&I product companies, parts suppliers, tech startups, software vendors and other businesses.
- outlet: the publication's usual name (for example "The Edge Malaysia" for theedgemalaysia.com, "Automotive News" for autonews.com). If unsure, repeat the publisher given.
- Return one result per input item, with the same id."""


OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "relevant", "signal", "companies", "brands", "direction", "summary", "implication", "confidence",
                     "region", "english", "current", "outlet", "buyer"],
        "properties": {
            "id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "signal": {"anyOf": [{"type": "string", "enum": SIGNALS}, {"type": "null"}]},
            "companies": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "role"],
                                                     "properties": {"name": {"type": "string"}, "role": {"type": "string", "enum": ROLES}}}},
            "brands": {"type": "array", "items": {"type": "string"}},
            "direction": {"anyOf": [{"type": "string", "enum": ["rising", "falling"]}, {"type": "null"}]},
            "summary": {"type": "string"},
            "implication": {"type": "string"},
            "confidence": {"type": "number"},
            "region": {"type": "string", "enum": STORY_REGIONS},
            "english": {"type": "boolean"},
            "current": {"type": "boolean"},
            "outlet": {"type": "string"},
            "buyer": {"type": "boolean"},
        }}}},
}
FORBIDDEN = re.compile(r"\b(should|reach out|contact|opportunity to)\b", re.I)


class BatchFailed(Exception):
    pass


GENERIC_NAME_WORDS = {"the", "and", "group", "motor", "motors", "auto", "autos", "automotive", "automobile", "automobiles",
                      "holdings", "holding", "international", "company", "corporation", "corp", "inc", "ltd", "llc", "plc",
                      "limited", "bhd", "berhad", "co", "industries", "global", "dealership", "dealer", "cars"}


def name_in_text(name: str, text: str) -> bool:
    """Guard against made-up names: the name, or at least one distinctive word of it, must be in the article.
    "Jardine Matheson" passes for a story that says "Jardine's"; a company with no word in the text doesn't."""
    n = name.lower().strip().replace("’", "'")
    t = text.lower().replace("’", "'")
    if not n:
        return False
    core = re.sub(r"[,.]?\s+(inc|ltd|llc|plc|corp|corporation|co|limited)\.?$", "", n)
    if n in t or (len(core) >= 2 and core in t):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", core) if len(w) >= 3 and w not in GENERIC_NAME_WORDS]
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in words)


def ai_tag_batch(client, rules: dict, batch: list[dict]) -> tuple[dict, list[str]]:
    """Tag one batch with Claude. Returns ({story_id: tag}, rejected ids). Raises BatchFailed."""
    import anthropic

    ai = rules["settings"]["ai"]
    payload = [{"id": s["id"], "title": s["title"], "snippet": s["snippet"], "publisher": s["publisher"], "publishedAt": s["published"].isoformat()} for s in batch]
    try:
        res = client.messages.create(
            model=ai["model"],
            max_tokens=ai["max_tokens"],
            output_config={"effort": ai["effort"], "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=[{"type": "text", "text": system_prompt(rules), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps({"today": dt.datetime.now(dt.timezone.utc).date().isoformat(), "items": payload})}],
        )
    except anthropic.RateLimitError as e:
        raise BatchFailed(f"rate limited: {e}")
    except anthropic.APIStatusError as e:
        raise BatchFailed(f"API error {e.status_code}: {e}")
    except anthropic.APIConnectionError as e:
        raise BatchFailed(f"connection error: {e}")
    if res.stop_reason in ("refusal", "max_tokens"):
        raise BatchFailed(f"stop_reason {res.stop_reason}")
    text = next((b.text for b in res.content if b.type == "text"), None)
    try:
        results = json.loads(text)["results"]
    except Exception as e:  # noqa: BLE001
        raise BatchFailed(f"malformed output: {e}")

    by_id = {s["id"]: s for s in batch}
    tags, rejected = {}, []
    for r in results:
        s = by_id.get(r.get("id"))
        if not s:
            continue
        ok = (r.get("signal") in SIGNALS or r.get("signal") is None) and isinstance(r.get("companies"), list) and isinstance(r.get("confidence"), (int, float))
        src = f"{s['title']} {s['snippet']}"
        names = [c.get("name", "") for c in r.get("companies", [])] + list(r.get("brands", []))
        if not ok or not all(name_in_text(n, src) for n in names):
            rejected.append(s["id"])  # a name the article doesn't contain: fall back to rules
            continue
        usable = bool(r["relevant"]) and r["signal"] is not None and r.get("english", True) is not False and r.get("current", True) is not False
        tags[s["id"]] = {
            "relevant": usable,
            "signal": r["signal"] if usable else None,
            "companies": [{"name": c["name"], "role": c["role"] if c.get("role") in ROLES else "subject"} for c in r["companies"]],
            "brands": list(r.get("brands", [])),
            "direction": (r.get("direction") or "rising") if r["signal"] in ("sales_shift", "oem_retail_strategy") else None,
            "summary": r.get("summary", "").strip(),
            "implication": "" if FORBIDDEN.search(r.get("implication", "")) else r.get("implication", "").strip(),
            "confidence": max(0.0, min(1.0, float(r["confidence"]))),
            "region": r.get("region") if r.get("region") in STORY_REGIONS else "Unknown",
            "outlet": (r.get("outlet") or "").strip(),
            "buyer": r.get("buyer") if isinstance(r.get("buyer"), bool) else None,
            "tagged_by": "ai",
        }
        del by_id[s["id"]]
    rejected += [i for i in by_id if i not in rejected]  # skipped by the model
    return tags, rejected


def merge_same_story(stories: list[dict], tags: dict, window_hours: int = 48) -> list[dict]:
    """Headlines too different to match by words can still be one story: same signal, overlapping companies."""
    kept: list[dict] = []
    for st in sorted(stories, key=lambda s: (s["tier"], s["published"])):
        t = tags[st["id"]]
        cos = {norm_company(c["name"]) for c in t["companies"]}
        twin = None
        if t["relevant"] and cos:
            for k in kept:
                tk = tags[k["id"]]
                kc = {norm_company(c["name"]) for c in tk["companies"]}
                if tk["signal"] == t["signal"] and kc and jaccard(cos, kc) >= 0.5 and abs((k["published"] - st["published"]).total_seconds()) <= window_hours * 3600:
                    twin = k
                    break
        if twin:
            twin["publishers"] |= st["publishers"]
            twin["urls"] |= st["urls"]
            twin["outlets"] = len(twin["publishers"])
        else:
            kept.append(st)
    return kept


def classify(stories: list[dict], rules: dict, ctx: RulesContext, client=None) -> tuple[dict, int]:
    """Tag each story once. AI per batch (retry once), keyword rules if a batch fails. Returns (tags, fallback batches)."""
    for i, s in enumerate(stories):
        s["id"] = str(i)
    tags: dict = {}
    fallback = 0
    if not client:
        tags = {s["id"]: classify_rules(s, ctx) for s in stories}
    else:
        size = rules["settings"]["ai"]["batch_size"]
        batches = [stories[i:i + size] for i in range(0, len(stories), size)]

        def run(batch):
            for attempt in range(2):
                try:
                    return ai_tag_batch(client, rules, batch), None
                except BatchFailed as e:
                    err = str(e)
                    log(f"  AI batch failed (attempt {attempt + 1}): {err}")
            return None, err

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for batch, (result, _err) in zip(batches, pool.map(run, batches)):
                if result is None:
                    fallback += 1
                    tags.update({s["id"]: classify_rules(s, ctx) for s in batch})
                    continue
                got, rejected = result
                tags.update(got)
                tags.update({s["id"]: classify_rules(s, ctx) for s in batch if s["id"] in rejected})
    return tags, fallback


# ---------------------------------------------------------------- route and rank (design doc, section 4)

# signal -> section for stories that aren't about us or a competitor. None = leave out.
ROUTING = {
    "acquisition": "account_news", "closure": "account_news", "new_store": "account_news", "market_entry": "account_news",
    "competitor_selected": "account_news", "leadership_change": "account_news", "tech_investment": "account_news",
    "oem_dms_certification": "industry_trends", "oem_retail_strategy": "industry_trends", "sales_shift": "industry_trends",
    "launch": "industry_trends", "regulation": "industry_trends", "market_trend": "industry_trends",
    "major_recall": "industry_trends",  # only recalls of 100,000+ vehicles (see the AI instructions)
    # About Tekion or a listed competitor -> Tekion / Competitors (see route). Other dealer software companies
    # (a voice-AI startup winning dealers, say) are a dealer-tech trend.
    "competitor_incident": "industry_trends", "vendor_news": "industry_trends",
}


def _names(entry) -> set[str]:
    return {norm_company(n) for n in [entry["name"]] + entry["aliases"]}


def vendor_kind(tag: dict, rules: dict) -> tuple[str, str] | None:
    """("us", "Tekion") or ("competitor", name) when the story is about us or a competing software provider."""
    us = {"name": rules["us"]["name"], "aliases": rules["us"].get("aliases", [])}
    found = None
    for c in tag["companies"]:
        if c["role"] == "other":
            continue
        n = norm_company(c["name"])
        if n in _names(us):
            return "us", us["name"]
        for comp in rules["competitors"]:
            if not found and n in _names({"name": comp["name"], "aliases": comp.get("aliases", [])}):
                found = ("competitor", comp["name"])
    return found


def in_regions(tag: dict, regions: list[str] | None) -> bool:
    return not regions or tag.get("region") in (None, "Global", "Unknown") or tag["region"] in regions


def route(tag: dict, rules: dict, regions: list[str] | None = None) -> str | None:
    """Section for a tagged story, or None to leave it out.
    - About Tekion -> Tekion; about a competing provider -> Competitors. From any region.
    - A dealer choosing a competitor -> Sales signals, labelled with the dealer (it needs a named dealer).
    - Sales signals are for businesses that would buy dealer software (buyer). The same kind of news about any
      other business (an insurer buying a warranty company, a tech startup raising money) goes to Industry trends.
    - Industry trends: market-wide developments in the team's regions."""
    if not tag["relevant"] or not tag["signal"]:
        return None
    signal = tag["signal"]
    vendor = vendor_kind(tag, rules)
    if vendor and (vendor[0] == "us" or signal != "competitor_selected"):
        return "tekion" if vendor[0] == "us" else "competitors"
    section = ROUTING[signal]
    if section is None or not in_regions(tag, regions):
        return None
    if section == "account_news" and tag.get("buyer") is False:
        return "industry_trends"
    if signal == "competitor_selected" and not dealer_name(tag, rules):
        return None
    return section


def dealer_name(tag: dict, rules: dict) -> str:
    """The dealer in a story that also names a software provider (the first named company that isn't one)."""
    for co in tag["companies"]:
        if co["role"] != "other" and not vendor_kind({"companies": [co]}, rules):
            return co["name"]
    return ""


def lead_name(c: dict, rules: dict) -> str:
    """The company an item is about, for its label."""
    if c["tag"]["signal"] == "competitor_selected" and c["section"] == "account_news":
        return dealer_name(c["tag"], rules)
    vendor = vendor_kind(c["tag"], rules)
    if vendor:
        return vendor[1]
    for role in ("acquirer", "subject", "target", "certified"):
        for co in c["tag"]["companies"]:
            if co["role"] == role:
                return co["name"]
    return (c["tag"]["brands"] or [""])[0]


def rank(candidates: list[dict], rules: dict) -> dict:
    """Candidates already carry their section (see route). Here: labels and ordering by sales signal (settings.signal_order). Optional limits, all
    off when unset: a hot-news gate on confidence, one item per type in Industry trends, a per-outlet cap and section caps. Nothing is marked
    as a priority: the team decides that."""
    s = rules["settings"]
    order = {sig: i for i, sig in enumerate(s.get("signal_order") or SIGNALS)}
    out = {sec: [] for sec in SECTIONS}
    for c in candidates:
        if s.get("hot_news_gate") and c["tag"]["confidence"] < s["hot_news_gate"]:  # off unless set
            continue
        # Label: company · region (region left out when unknown).
        region = c["tag"].get("region") or ""
        c["lead"] = " · ".join(x for x in (lead_name(c, rules), region if region != "Unknown" else "") if x)
        out[c["section"]].append(c)
    account_signals = {sig for sig, sec in ROUTING.items() if sec == "account_news"}
    per_outlet: dict[str, int] = {}
    for sec in SECTIONS:
        if sec == "industry_trends":
            # Market trends before company news about non-buyers (an insurer buying a warranty firm); then the
            # most widely covered story first; ties by trusted source, event type and recency.
            out[sec].sort(key=lambda c: (c["tag"]["signal"] in account_signals, -c["story"]["outlets"], c["story"]["tier"],
                                         order.get(c["tag"]["signal"], len(order)), -c["story"]["published"].timestamp()))
            # Variety: the best story of each type first, then the rest in the same order.
            seen_types: set[str] = set()
            first = [c for c in out[sec] if not (c["tag"]["signal"] in seen_types or seen_types.add(c["tag"]["signal"]))]
            out[sec] = first + [c for c in out[sec] if c not in first]
        else:
            out[sec].sort(key=lambda c: (order.get(c["tag"]["signal"], len(order)),
                                         c["story"]["tier"], -c["story"]["outlets"], -c["story"]["published"].timestamp()))
        kept, types = [], set()
        for c in out[sec]:
            if sec == "industry_trends" and s.get("trends_one_per_type"):  # three launches can't fill the section
                if c["tag"]["signal"] in types:
                    continue
                types.add(c["tag"]["signal"])
            host = c["story"]["publisher"] if c["story"].get("is_aggregator") else (urlsplit(c["story"]["url"]).hostname or c["story"]["publisher"])
            if s.get("max_per_outlet") and per_outlet.get(host, 0) >= s["max_per_outlet"]:
                continue
            per_outlet[host] = per_outlet.get(host, 0) + 1
            kept.append(c)
        cap = (s.get("caps") or {}).get(sec)
        out[sec] = kept[:cap] if cap is not None else kept
    return out


# ---------------------------------------------------------------- compose

def _sentence(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?" else s + "."


def _outlet(story: dict, tag: dict) -> str:
    """The publication's name. Exa items carry a bare domain; the AI supplies the usual name when it can."""
    pub = story["publisher"]
    return tag.get("outlet") or pub if "." in pub else pub


def compose(sections: dict, local_now: dt.datetime, window_start: dt.datetime | None, degraded: bool,
            contact: str = "") -> tuple[str, str, str]:
    date_label = f"{local_now:%A}, {local_now.day} {local_now:%B}"
    total = sum(len(v) for v in sections.values())
    if window_start is None or (local_now - window_start.astimezone(local_now.tzinfo)) <= dt.timedelta(hours=24.5):
        period = "in the last 24 hours"
    else:
        ws = window_start.astimezone(local_now.tzinfo)
        period = f"since the last update on {ws:%A}, {ws.day} {ws:%B}"

    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    head = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>'
            f'<body style="margin:0;padding:0;background:#ffffff;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="padding:24px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:640px;{font}font-size:15px;line-height:1.5;color:#1f2328;"><tr><td>')
    who = contact or "the team that runs this report"
    footer_line = f"This is an automated email, so replies aren’t read. To share feedback, message {who} on Slack or by email."
    footer_html = f'<p style="margin:28px 0 0;color:#57606a;font-size:13px;">{html.escape(footer_line)}</p>'
    tail = "</td></tr></table></td></tr></table></body></html>"
    partial_html = '<p style="margin:0 0 16px;color:#57606a;font-size:13px;">Coverage was partial today.</p>' if degraded else ""
    partial_text = "Coverage was partial today.\n\n" if degraded else ""

    subject = f"Automotive market update – {date_label}"
    if total == 0:
        line = f"Nothing relevant happened in the automotive world {period}."
        return (subject, f'{head}<p style="margin:0 0 16px;">Hi Team,</p>{partial_html}<p style="margin:0;">{html.escape(line)}</p>{footer_html}{tail}',
                f"Hi Team,\n\n{partial_text}{line}\n\n{footer_line}\n")

    intro = f"Here's what happened in the automotive world {period}."
    h = [head, '<p style="margin:0 0 16px;">Hi Team,</p>', partial_html, f'<p style="margin:0 0 8px;">{html.escape(intro)}</p>']
    t = ["Hi Team,", "", partial_text + intro, ""]
    seen_impl: set[str] = set()
    for sec in SECTIONS:
        if not sections[sec]:
            continue
        h.append(f'<p style="margin:24px 0 8px;font-weight:bold;">{SECTION_TITLES[sec]}</p>')
        t += [SECTION_TITLES[sec].upper(), ""]
        for c in sections[sec]:
            story, tag = c["story"], c["tag"]
            fact = _sentence(tag["summary"] or story["title"])
            lead = c.get("lead", "")
            impl = _sentence(tag["implication"]) if tag["implication"] else ""
            if impl.lower() in seen_impl:
                impl = ""  # never print the same line twice in one email
            seen_impl.add(impl.lower())
            outlet = _outlet(story, tag)
            h.append(
                '<p style="margin:0 0 14px;">' + (f"<strong>{html.escape(lead)}</strong> – " if lead else "") + html.escape(fact)
                + f' (<a href="{html.escape(story["url"], quote=True)}" style="color:#0b5cad;">{html.escape(outlet)}</a>)'
                + (f'<br><em style="color:#424a53;">Why it matters: {html.escape(impl)}</em>' if impl else "") + "</p>"
            )
            t.append(f"- {lead + ' – ' if lead else ''}{fact} ({outlet}: {story['url']})")
            if impl:
                t.append(f"   Why it matters: {impl}")
            t.append("")
    h.append(footer_html)
    h.append(tail)
    t.append(footer_line)
    return subject, "".join(h), "\n".join(t)


# ---------------------------------------------------------------- send

def send_email(recipients: list[str], subject: str, html_body: str, text_body: str) -> None:
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not user or not password:
        raise RuntimeError("SMTP_USER and SMTP_PASS must be set to send email (for Gmail, SMTP_PASS is an app password)")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)


# ---------------------------------------------------------------- state (sent.json)

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"last_sent_date": None, "last_sent_at": None, "sent_items": [], "runs": []}


def save_state(path: Path, state: dict, now: dt.datetime, rules: dict) -> None:
    s = rules["settings"]
    keep_sent = (now - dt.timedelta(days=s["sent_memory_days"])).isoformat()
    state["sent_items"] = [x for x in state["sent_items"] if x["at"] >= keep_sent]
    state.pop("history", None)  # no longer used
    state["runs"] = state["runs"][-30:]
    path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- run

def build_report(cfg: dict, rules: dict, state: dict, now: dt.datetime, fetcher=None, ai_client=None) -> dict:
    s = rules["settings"]
    default_start = now - dt.timedelta(hours=s["lookback_hours"])
    max_start = now - dt.timedelta(hours=s["max_lookback_hours"])
    last = dt.datetime.fromisoformat(state["last_sent_at"]) if state.get("last_sent_at") else None
    # A missed day widens the window to cover everything since the last email (capped).
    window_start = max(max_start, min(last, default_start)) if last else default_start

    if cfg.get("regions"):
        # Point the Exa searches at the team's regions ("... in the United States or Canada").
        hint = " in " + " or ".join(REGION_HINTS[r] for r in cfg["regions"])
        rules = {**rules, "sources": [{**src, "query": src["query"] + hint} if src["type"] == "exa" else src for src in rules["sources"]]}
    items, ok, failed = collect(rules, window_start, now, fetcher)
    attempted = len(ok) + len(failed)
    log(f"Collect: {len(ok)}/{attempted} sources ok, {len(items)} items in window" + (f", failed: {', '.join(f['id'] for f in failed)}" if failed else ""))
    items = rate_outlets(normalize(items), rules)
    items, dropped = prefilter(items, rules)
    log(f"Clean: {len(items)} kept, {dropped} dropped as noise or off-topic")
    tz = ZoneInfo(cfg["timezone"])
    today = now.astimezone(tz).date()
    # Only emails from earlier days count as already sent; a second send the same day keeps today's stories.
    earlier = [x for x in state["sent_items"] if dt.datetime.fromisoformat(x["at"]).astimezone(tz).date() < today] \
        if s["sent_memory_days"] else []  # 0 = off: every story in the window is sent, even if sent before
    stories = drop_already_sent(dedupe(items, s["title_similarity"]), earlier, s["title_similarity"])
    log(f"Dedupe: {len(stories)} new stories")
    ctx = RulesContext(rules)
    tags, fallback = classify(stories, rules, ctx, ai_client)
    stories = merge_same_story(stories, tags)
    relevant = [st for st in stories if tags[st["id"]]["relevant"]]
    log(f"Tag: {len(relevant)} relevant ({'AI' if ai_client else 'keyword rules'}{f', {fallback} batch(es) fell back to rules' if fallback else ''})")

    candidates = []
    for st in relevant:
        sec = route(tags[st["id"]], rules, cfg.get("regions"))
        if sec:
            candidates.append({"story": st, "tag": tags[st["id"]], "section": sec})
    sections = rank(candidates, rules)
    degraded = len(ok) < 0.4 * attempted or fallback > 0
    local_now = now.astimezone(ZoneInfo(cfg["timezone"]))
    subject, html_body, text_body = compose(sections, local_now, window_start if last else None, degraded, cfg.get("contact", ""))
    included = [c for sec in SECTIONS for c in sections[sec]]
    log(f"Email: {len(included)} items ({', '.join(f'{SECTION_TITLES[k]} {len(v)}' for k, v in sections.items() if v) or 'quiet day'})")
    return {"subject": subject, "html": html_body, "text": text_body, "included": included, "relevant": relevant, "tags": tags,
            "degraded": degraded, "failed_sources": failed, "fallback_batches": fallback}


def make_ai_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=45)


def load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env for local runs. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv=None, now: dt.datetime | None = None, fetcher=None, ai_client=None, sender=send_email) -> int:
    p = argparse.ArgumentParser(description="Daily Automotive Sales Signals")
    p.add_argument("--preview", action="store_true", help="build the email now and write it to out/; send nothing")
    p.add_argument("--send-now", action="store_true", help="build and send now, ignoring the send hour")
    p.add_argument("--to", help="with --send-now: send only to this address (a test copy)")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--rules", default=str(ROOT / "rules.yaml"))
    p.add_argument("--state", default=str(ROOT / "sent.json"))
    p.add_argument("--out", default=str(ROOT / "out"))
    args = p.parse_args(argv)

    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        cfg, rules = load_config(Path(args.config)), load_rules(Path(args.rules))
    except ConfigError as e:
        log(f"Config error: {e}")
        return 2
    state_path = Path(args.state)
    state = load_state(state_path)
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    today = local.date().isoformat()

    if not args.preview and not args.send_now:
        due_until = cfg["send_hour"] + rules["settings"]["retry_hours"]
        if not cfg["send_hour"] <= local.hour < due_until:
            log(f"Not due: it's {local:%H:%M} in {cfg['timezone']}; the email goes out at {cfg['send_hour']:02d}:00.")
            return 0
        if state.get("last_sent_date") == today:
            log(f"Already sent today ({today}).")
            return 0

    run = {"at": now.isoformat(), "mode": "preview" if args.preview else "send"}
    try:
        report = build_report(cfg, rules, state, now, fetcher, make_ai_client() if ai_client is None else ai_client)
        if args.preview:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "preview.html").write_text(report["html"], encoding="utf-8")
            (out / "preview.txt").write_text(f"Subject: {report['subject']}\n\n{report['text']}", encoding="utf-8")
            log(f"Preview written to {out / 'preview.html'} and preview.txt. Nothing was sent.")
            return 0
        recipients = [args.to] if args.to else cfg["recipients"]
        sender(recipients, report["subject"], report["html"], report["text"])
        log(f"Sent to {', '.join(recipients)}.")
        if not args.to:  # a test copy doesn't count as today's email
            state["last_sent_date"] = today
            state["last_sent_at"] = now.isoformat()
            for c in report["included"]:
                st = c["story"]
                state["sent_items"].append({"url": st["canonical"], "tokens": st["tokens"], "at": now.isoformat()})
        run.update(status="degraded" if report["degraded"] else "ok", items=len(report["included"]), recipients=len(recipients),
                   failed_sources=[f["id"] for f in report["failed_sources"]])
        return 0
    except Exception as e:  # noqa: BLE001 - record, then fail the job so GitHub alerts the owner
        run.update(status="failed", error=str(e)[:300])
        log(f"Run failed: {e}")
        return 1
    finally:
        if not args.preview:
            state["runs"].append(run)
            save_state(state_path, state, now, rules)


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    sys.exit(main())
