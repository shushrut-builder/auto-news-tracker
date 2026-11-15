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

SECTIONS = ["potential_risk", "potential_growth", "new_business", "industry_development", "lower_priority"]
SECTION_TITLES = {
    "potential_risk": "Potential risk",
    "potential_growth": "Potential growth",
    "new_business": "New business",
    "industry_development": "Industry development",
    "lower_priority": "Lower priority",
}
ACTION_SECTIONS = {"potential_risk", "potential_growth", "new_business"}
SIGNALS = [
    "acquisition", "closure", "new_store", "market_entry", "competitor_selected", "leadership_change", "tech_investment",
    "oem_dms_certification", "oem_retail_strategy", "major_recall", "sales_shift", "launch",
    "competitor_incident", "regulation",
]
BRAND_SIGNALS = {"oem_dms_certification", "oem_retail_strategy", "major_recall", "sales_shift", "launch"}
ROLES = ["acquirer", "target", "subject", "certified", "other"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- config

class ConfigError(Exception):
    pass


def parse_watchlist(lines) -> list[dict]:
    """["Name | alias1, alias2", ...] -> [{"name", "aliases"}], blanks and duplicates ignored."""
    out, seen = [], set()
    for raw in lines or []:
        line = str(raw).strip()
        if not line:
            continue
        name, _, aliases = line.partition("|")
        name = name.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "aliases": [a.strip() for a in aliases.split(",") if a.strip()]})
    return out


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
    return {"recipients": [str(r) for r in recipients], "send_hour": hour, "timezone": tz, "watchlist": parse_watchlist(cfg.get("watchlist"))}


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
            "implication": spec["implication"],
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
                    items.append(it)
    return items, ok, failed


# ---------------------------------------------------------------- clean: normalise, pre-filter, dedupe

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
    def __init__(self, rules: dict, watchlist: list[dict]):
        self.rules = rules
        known = []
        for c in [rules["us"]] + rules["competitors"]:
            kind = "us" if c is rules["us"] else "competitor"
            for n in [c["name"]] + c.get("aliases", []):
                known.append((c["name"], word_re(n), kind))
        for w in watchlist:
            for n in [w["name"]] + w["aliases"]:
                known.append((w["name"], word_re(n), "watch"))
        known.sort(key=lambda k: -len(k[1].pattern))
        self.known = known
        names = list(dict.fromkeys(rules["brands"] + [w["name"] for w in watchlist]))
        self.brands = sorted(((n, word_re(n)) for n in names), key=lambda b: -len(b[0]))


def classify_rules(story: dict, ctx: RulesContext) -> dict:
    title, text = story["title"], f"{story['title']} {story['snippet']}"
    signal, in_title, implication, rising = None, False, "", None
    for target, flag in ((title, True), (text, False)):
        for s in ctx.rules["_signals"]:
            if any(p.search(target) for p in s["patterns"]):
                signal, in_title, implication, rising = s["signal"], flag, s["implication"], s["rising"]
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
            "summary": summary, "implication": implication, "confidence": confidence, "tagged_by": "rules"}


# ---------------------------------------------------------------- classify: AI (optional)

SIGNAL_DEFS = """Company signals (about a specific dealer group, dealer, distributor or OEM):
- acquisition: a company buys, merges with, or is bought by another (dealerships, stores, groups). Give roles: acquirer and target.
- closure: a store/dealership closes, a franchise is lost or terminated, a group goes bankrupt.
- new_store: a new dealership, showroom or store opens or breaks ground.
- market_entry: a brand or company enters a new country/market, or appoints a distributor/importer.
- competitor_selected: a dealer or group selects, signs with, or rolls out a competing dealer software provider (listed below).
- leadership_change: a senior executive (CEO, CIO, CTO, CFO, COO, president, head of digital/IT) is appointed or leaves.
- tech_investment: a dealer, group or OEM invests in retail technology, digital retail, AI, software or IT modernisation.
Brand signals (about a vehicle brand / OEM, affecting its dealers):
- oem_dms_certification: an OEM certifies or approves a dealer management system (DMS) vendor for its dealers. Mark the certified vendor with role "certified".
- oem_retail_strategy: an OEM changes how its vehicles are sold (agency model, direct sales, dealer network changes, dealer standards).
- major_recall: a large vehicle recall.
- sales_shift: a notable rise or fall in a brand's or market's vehicle sales, registrations or market share. Set direction.
- launch: a new model or EV launch, or a launch date announced.
Market signals:
- competitor_incident: an outage, cyberattack, breach, lawsuit or regulatory action involving a competing dealer software provider.
- regulation: a law, regulation, tariff or policy change affecting auto dealers or vehicle sales."""


def system_prompt(rules: dict) -> str:
    us = ", ".join([rules["us"]["name"]] + rules["us"].get("aliases", []))
    comps = "; ".join(" / ".join([c["name"]] + c.get("aliases", [])) for c in rules["competitors"])
    return f"""You tag automotive news items for a business development team that sells dealer management and automotive retail software to carmakers, distributors and dealer groups.

Our company: {us}.
Competing dealer software providers: {comps}.

For each item, decide whether it is relevant to B2B automotive retail (dealers, dealer groups, distributors, OEM retail and sales, dealer software). Consumer content (car reviews, buying advice, motorsport, deals, celebrity, general business news unrelated to auto retail) is relevant: false, with signal null.

Signals (choose exactly one for relevant items, the best fit):
{SIGNAL_DEFS}

Company roles: acquirer, target (the one being bought or getting new owners), certified, subject (the company the news is about), other.

Rules:
- Extract only companies and brands that are named in the title or snippet. Use the name as written. Never infer or add names.
- companies are businesses (dealer groups, dealers, OEMs as corporate actors, vendors). brands are vehicle brands. An OEM can appear in both.
- summary: the fact only, in one plain sentence of at most 35 words. No opinion, no implication.
- implication: what this kind of event typically means for a company selling dealer software, in one sentence of at most 25 words. Never say when or how to act. Never use the words "should", "reach out", "contact", "opportunity to". Formal, plain language.
- confidence: 0 to 1, your confidence that the signal, companies and roles are correct given only the text. Use below 0.6 when the text is thin or ambiguous.
- direction: "rising" or "falling" for sales_shift, otherwise null.
- Return one result per input item, with the same id."""


OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "relevant", "signal", "companies", "brands", "direction", "summary", "implication", "confidence"],
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
        }}}},
}
FORBIDDEN = re.compile(r"\b(should|reach out|contact|opportunity to)\b", re.I)


class BatchFailed(Exception):
    pass


def name_in_text(name: str, text: str) -> bool:
    n, t = name.lower().strip(), text.lower()
    core = re.sub(r"[,.]?\s+(inc|ltd|llc|plc|corp|corporation|co|limited)\.?$", "", n)
    return bool(n) and (n in t or (len(core) >= 2 and core in t))


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
            messages=[{"role": "user", "content": json.dumps(payload)}],
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
        tags[s["id"]] = {
            "relevant": bool(r["relevant"]) and r["signal"] is not None,
            "signal": r["signal"] if r["relevant"] else None,
            "companies": [{"name": c["name"], "role": c["role"] if c.get("role") in ROLES else "subject"} for c in r["companies"]],
            "brands": list(r.get("brands", [])),
            "direction": (r.get("direction") or "rising") if r["signal"] == "sales_shift" else None,
            "summary": r.get("summary", "").strip(),
            "implication": "" if FORBIDDEN.search(r.get("implication", "")) else r.get("implication", "").strip(),
            "confidence": max(0.0, min(1.0, float(r["confidence"]))),
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
    by_signal = {s["signal"]: s["implication"] for s in rules["_signals"]}
    for t in tags.values():
        if t["signal"] and not t["implication"]:
            t["implication"] = by_signal.get(t["signal"], "")
    return tags, fallback


# ---------------------------------------------------------------- route and rank (design doc, section 4)

FALL = "fall"  # this column doesn't decide; try the next one


def _acq(ctx):
    return "potential_growth" if ctx["role"] == "acquirer" else "potential_risk"


def _cert_brand(ctx):
    return {"competitor": "potential_risk", "us": "potential_growth"}.get(ctx["cert"], FALL)


def _cert_other(ctx):
    return "new_business" if ctx["cert"] == "us" else "industry_development"


def _sales(ctx):
    return "potential_risk" if ctx["direction"] == "falling" else "potential_growth"


# signal: (watched company, watched brand, not on the watchlist). None = don't show.
ROUTING = {
    "acquisition": (_acq, FALL, "new_business"),
    "closure": ("potential_risk", FALL, "lower_priority"),
    "new_store": ("potential_growth", FALL, "new_business"),
    "market_entry": ("potential_growth", FALL, "new_business"),
    "competitor_selected": ("potential_risk", FALL, "lower_priority"),
    "leadership_change": ("potential_risk", FALL, "new_business"),
    "tech_investment": ("potential_growth", FALL, "new_business"),
    "oem_dms_certification": (FALL, _cert_brand, _cert_other),
    "oem_retail_strategy": (FALL, "potential_growth", "industry_development"),
    "major_recall": (FALL, "potential_growth", None),
    "sales_shift": (FALL, _sales, "industry_development"),
    "launch": (FALL, "potential_growth", "industry_development"),
    "competitor_incident": ("industry_development", "industry_development", "new_business"),
    "regulation": ("industry_development", "industry_development", "industry_development"),
}
ROLE_ORDER = ["acquirer", "target", "certified", "subject", "other"]


def _names(entry) -> set[str]:
    return {norm_company(n) for n in [entry["name"]] + entry["aliases"]}


def match_watchlist(tag: dict, watchlist: list[dict]) -> tuple[dict | None, dict | None]:
    """(company match {name, role}, brand match {name}). One watchlist: a name can match either way."""
    company_hit = brand_hit = None
    # Normalised names only match whole names, so "HD Hyundai" never equals "Hyundai" here.
    brands = {norm_company(b) for b in tag["brands"]}
    for w in watchlist:
        names = _names(w)
        hits = [c for c in tag["companies"] if norm_company(c["name"]) in names]
        if hits and not company_hit:
            hits.sort(key=lambda c: ROLE_ORDER.index(c["role"]) if c["role"] in ROLE_ORDER else 9)
            company_hit = {"name": w["name"], "role": hits[0]["role"]}
        if (names & brands or hits) and not brand_hit:
            brand_hit = {"name": w["name"]}
    return company_hit, brand_hit


def cert_party(tag: dict, rules: dict) -> str | None:
    certified = [norm_company(c["name"]) for c in tag["companies"] if c["role"] == "certified"]
    if not certified:
        return None
    for n in certified:
        if any(n in _names({"name": c["name"], "aliases": c.get("aliases", [])}) for c in rules["competitors"]):
            return "competitor"
        if n in _names({"name": rules["us"]["name"], "aliases": rules["us"].get("aliases", [])}):
            return "us"
    return "other"


def route(tag: dict, watchlist: list[dict], rules: dict) -> tuple[str, str | None] | None:
    """Section for a tagged story and the watchlist name that matched (if any). None = leave out."""
    if not tag["relevant"] or not tag["signal"]:
        return None
    company_cell, brand_cell, other_cell = ROUTING[tag["signal"]]
    ctx = {"role": None, "cert": cert_party(tag, rules), "direction": tag["direction"]}
    company_hit, brand_hit = match_watchlist(tag, watchlist)

    def cell(c, extra=None):
        return c({**ctx, **(extra or {})}) if callable(c) else c

    if company_hit:
        r = cell(company_cell, {"role": company_hit["role"]})
        if r is None:
            return None
        if r != FALL:
            return r, company_hit["name"]
    if brand_hit:
        r = cell(brand_cell)
        if r is None:
            return None
        if r != FALL:
            return r, brand_hit["name"]
    r = cell(other_cell)
    return (r, None) if r and r != FALL else None


def rank(candidates: list[dict], rules: dict, history: list[dict]) -> dict:
    """Confidence gate, Priority, ordering and caps. `history` is [{company, key, at}] from earlier runs."""
    s = rules["settings"]
    brand_names = {norm_company(b) for b in rules["brands"]}
    counts: dict[str, set] = {}
    for h in history:
        counts.setdefault(h["company"], set()).add(h["key"])
    for c in candidates:
        for co in c["tag"]["companies"]:
            counts.setdefault(norm_company(co["name"]), set()).add(c["story"]["canonical"])
    out = {sec: [] for sec in SECTIONS}
    for c in candidates:
        section = c["section"]
        if section in ACTION_SECTIONS and c["tag"]["confidence"] < s["confidence_gate"]:
            section = "industry_development"
        c["priority"] = any(
            norm_company(co["name"]) not in brand_names and len(counts.get(norm_company(co["name"]), ())) >= s["priority"]["min_signals"]
            for co in c["tag"]["companies"]
        )
        out[section].append(c)
    for sec in SECTIONS:
        out[sec].sort(key=lambda c: (not c["priority"], -c["tag"]["confidence"], c["story"]["tier"], -c["story"]["outlets"], -c["story"]["published"].timestamp()))
        cap = s["caps"].get(sec)
        if cap is not None:
            out[sec] = out[sec][:cap]
    return out


# ---------------------------------------------------------------- compose

def _sentence(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?" else s + "."


def compose(sections: dict, local_now: dt.datetime, window_start: dt.datetime | None, degraded: bool) -> tuple[str, str, str]:
    date_label = f"{local_now:%A}, {local_now.day} {local_now:%B}"
    total = sum(len(v) for v in sections.values())
    if window_start is None or (local_now - window_start.astimezone(local_now.tzinfo)) <= dt.timedelta(hours=24.5):
        period = "from the past 24 hours"
    else:
        ws = window_start.astimezone(local_now.tzinfo)
        period = f"since the last summary on {ws:%A}, {ws.day} {ws:%B}"

    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    head = (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>'
            f'<body style="margin:0;padding:0;background:#ffffff;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="padding:24px 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:640px;{font}font-size:15px;line-height:1.5;color:#1f2328;"><tr><td>')
    tail = "</td></tr></table></td></tr></table></body></html>"
    partial_html = '<p style="margin:0 0 16px;color:#57606a;font-size:13px;">Coverage was partial today.</p>' if degraded else ""
    partial_text = "Coverage was partial today.\n\n" if degraded else ""

    if total == 0:
        line = f"There were no relevant automotive developments {period.replace('from ', 'in ', 1)}."
        subject = f"Automotive market summary – {date_label} (no new developments)"
        return subject, f'{head}<p style="margin:0 0 16px;">Good morning,</p>{partial_html}<p style="margin:0;">{html.escape(line)}</p>{tail}', f"Good morning,\n\n{partial_text}{line}\n"

    subject = f"Automotive market summary – {date_label}"
    intro = f"Please find below the automotive developments {period} that are relevant to our accounts and markets."
    h = [head, '<p style="margin:0 0 16px;">Good morning,</p>', partial_html, f'<p style="margin:0 0 8px;">{html.escape(intro)}</p>']
    t = ["Good morning,", "", partial_text + intro, ""]
    for sec in SECTIONS:
        if not sections[sec]:
            continue
        h.append(f'<p style="margin:24px 0 8px;font-weight:bold;">{SECTION_TITLES[sec]}</p>')
        t += [SECTION_TITLES[sec].upper(), ""]
        for c in sections[sec]:
            story, tag = c["story"], c["tag"]
            fact = _sentence(tag["summary"] or story["title"])
            watch = f" {c['matched']} is on your watchlist." if c.get("matched") else ""
            prio = "Priority: " if c["priority"] else ""
            impl = _sentence(tag["implication"]) if tag["implication"] else ""
            h.append(
                f'<p style="margin:0 0 14px;">{"<strong>Priority:</strong> " if prio else ""}{html.escape(fact)}{html.escape(watch)} '
                f'(<a href="{html.escape(story["url"], quote=True)}" style="color:#0b5cad;">{html.escape(story["publisher"])}</a>)'
                + (f'<br><em style="color:#424a53;">Why it matters: {html.escape(impl)}</em>' if impl else "") + "</p>"
            )
            t.append(f"- {prio}{fact}{watch} ({story['publisher']}: {story['url']})")
            if impl:
                t.append(f"   Why it matters: {impl}")
            t.append("")
    h.append(tail)
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
    return {"last_sent_date": None, "last_sent_at": None, "sent_items": [], "history": [], "runs": []}


def save_state(path: Path, state: dict, now: dt.datetime, rules: dict) -> None:
    s = rules["settings"]
    keep_sent = (now - dt.timedelta(days=s["sent_memory_days"])).isoformat()
    keep_hist = (now - dt.timedelta(days=s["priority"]["window_days"])).isoformat()
    state["sent_items"] = [x for x in state["sent_items"] if x["at"] >= keep_sent]
    state["history"] = [x for x in state["history"] if x["at"] >= keep_hist]
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

    items, ok, failed = collect(rules, window_start, now, fetcher)
    attempted = len(ok) + len(failed)
    log(f"Collect: {len(ok)}/{attempted} sources ok, {len(items)} items in window" + (f", failed: {', '.join(f['id'] for f in failed)}" if failed else ""))
    items = normalize(items)
    items, dropped = prefilter(items, rules)
    log(f"Clean: {len(items)} kept, {dropped} dropped as noise or off-topic")
    stories = drop_already_sent(dedupe(items, s["title_similarity"]), state["sent_items"], s["title_similarity"])
    log(f"Dedupe: {len(stories)} new stories")
    ctx = RulesContext(rules, cfg["watchlist"])
    tags, fallback = classify(stories, rules, ctx, ai_client)
    stories = merge_same_story(stories, tags)
    relevant = [st for st in stories if tags[st["id"]]["relevant"]]
    log(f"Tag: {len(relevant)} relevant ({'AI' if ai_client else 'keyword rules'}{f', {fallback} batch(es) fell back to rules' if fallback else ''})")

    candidates = []
    for st in relevant:
        r = route(tags[st["id"]], cfg["watchlist"], rules)
        if r:
            candidates.append({"story": st, "tag": tags[st["id"]], "section": r[0], "matched": r[1]})
    sections = rank(candidates, rules, state["history"])
    degraded = len(ok) < 0.4 * attempted or fallback > 0
    local_now = now.astimezone(ZoneInfo(cfg["timezone"]))
    subject, html_body, text_body = compose(sections, local_now, window_start if last else None, degraded)
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
            for st in report["relevant"]:
                for co in report["tags"][st["id"]]["companies"]:
                    state["history"].append({"company": norm_company(co["name"]), "key": st["canonical"], "at": now.isoformat()})
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
