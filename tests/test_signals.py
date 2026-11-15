"""Tests for signals.py. No network and no email: feeds come from tests/fixtures, sending is faked,
and smtplib is blocked so an accidental real send fails the test."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import signals as S  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
RULES = S.load_rules(ROOT / "rules.yaml")
FIXTURE_SOURCES = [
    {"id": "cbt-news", "name": "CBT News", "url": "https://fixture.test/cbt.xml", "type": "rss", "tier": 2, "auto_only": True, "file": "cbt-news.xml"},
    {"id": "autocar-pro", "name": "Autocar Professional", "url": "https://fixture.test/acp.xml", "type": "rss", "tier": 2, "auto_only": True, "file": "autocar-pro.xml"},
    {"id": "gn-acquisition", "name": "Google News", "url": "https://fixture.test/gn.xml", "type": "google_news", "tier": 3, "auto_only": False, "file": "google-news.xml"},
]


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must never open an SMTP connection")

    monkeypatch.setattr(S.smtplib, "SMTP", boom)
    monkeypatch.setattr(S.smtplib, "SMTP_SSL", boom)


def fixture_rules():
    r = dict(RULES)
    r["sources"] = [{k: v for k, v in s.items() if k != "file"} for s in FIXTURE_SOURCES]
    return r


def fixture_fetcher(url):
    for s in FIXTURE_SOURCES:
        if url == s["url"]:
            return (FIX / s["file"]).read_bytes()
    raise RuntimeError(f"no fixture for {url}")


def fixture_now() -> dt.datetime:
    latest = max(it["published"] for s in FIXTURE_SOURCES for it in S.parse_feed((FIX / s["file"]).read_bytes(), s))
    return latest + dt.timedelta(hours=1)


def tag(**over):
    t = {"relevant": True, "signal": "acquisition", "companies": [], "brands": [], "direction": None,
         "summary": "Something happened.", "implication": "It typically means something.", "confidence": 0.9, "tagged_by": "ai"}
    t.update(over)
    return t


# ---------------------------------------------------------------- config

def test_watchlist_parsing():
    w = S.parse_watchlist(["AutoNation | AutoNation USA, AN", "", "Toyota", "toyota"])
    assert w == [{"name": "AutoNation", "aliases": ["AutoNation USA", "AN"]}, {"name": "Toyota", "aliases": []}]


def test_config_validation(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("recipients: [not-an-email]\nsend_hour: 8\ntimezone: America/Chicago\n", encoding="utf-8")
    with pytest.raises(S.ConfigError, match="recipients"):
        S.load_config(bad)
    bad.write_text("recipients: [a@b.com]\nsend_hour: 25\ntimezone: America/Chicago\n", encoding="utf-8")
    with pytest.raises(S.ConfigError, match="send_hour"):
        S.load_config(bad)
    bad.write_text("recipients: [a@b.com]\nsend_hour: 8\ntimezone: Mars/Base\n", encoding="utf-8")
    with pytest.raises(S.ConfigError, match="timezone"):
        S.load_config(bad)
    ok = tmp_path / "ok.yaml"
    ok.write_text("recipients: [a@b.com]\nsend_hour: 8\ntimezone: Asia/Kolkata\n", encoding="utf-8")
    assert S.load_config(ok)["watchlist"] == []  # the watchlist is optional


# ---------------------------------------------------------------- cleaning

def test_canonical_url():
    assert S.canonical_url("https://WWW.Example.COM/News/Story/?utm_source=x&id=5&fbclid=1#top") == "https://example.com/News/Story?id=5"
    assert S.canonical_url("http://a.com/x?b=2&a=1") == S.canonical_url("https://a.com/x?a=1&b=2")


def test_publisher_suffix_and_tokens():
    assert S.strip_publisher_suffix("Group buys three stores - Automotive News", "Automotive News") == "Group buys three stores"
    toks = S.title_tokens("Dealer group acquires five stores, report says")
    assert "5" in toks and "report" not in toks


def _item(title, url, tier=2, auto_only=True, publisher="Outlet", hours_ago=1):
    now = dt.datetime(2026, 11, 14, 12, tzinfo=dt.timezone.utc)
    return {"title": title, "url": url, "canonical": S.canonical_url(url), "tokens": S.title_tokens(title), "snippet": "",
            "tier": tier, "auto_only": auto_only, "publisher": publisher, "published": now - dt.timedelta(hours=hours_ago)}


def test_dedupe_clusters_paraphrases_and_picks_best_source():
    items = [
        _item("Lithia Motors acquires three Texas dealerships", "https://news.com/1", tier=3, publisher="Aggregator", hours_ago=3),
        _item("Lithia Motors acquired 3 Texas dealerships", "https://prn.com/2", tier=1, publisher="PR Newswire"),
        _item("Lithia Motors acquires three Texas dealerships", "https://www.news.com/1?utm_source=rss", tier=3),
        _item("Toyota recalls 62,000 Tundra pickups", "https://x.com/3"),
    ]
    stories = S.dedupe(items, 0.75)
    assert len(stories) == 2
    lithia = next(s for s in stories if "Lithia" in s["title"])
    assert lithia["publisher"] == "PR Newswire" and lithia["outlets"] == 2


def test_already_sent_is_dropped():
    stories = S.dedupe([_item("Asbury opens new store in Atlanta", "https://a.com/1")], 0.75)
    sent = [{"url": "https://other.com/x", "tokens": S.title_tokens("Asbury opens a new store in Atlanta"), "at": "2026-11-13T00:00:00+00:00"}]
    assert S.drop_already_sent(stories, sent, 0.75) == []


def test_prefilter_general_feeds_need_automotive_context():
    items = [
        _item("Advanta to strengthen presence with acquisition of Hytech", "https://a.com/1", auto_only=False),
        _item("Genesis BBQ opens first outlet in Africa", "https://a.com/2", auto_only=False),
        _item("HD Hyundai launches autonomous vessel", "https://a.com/3", auto_only=False),
        _item("Kia to invest $2 bln in India to ramp up production", "https://a.com/4", auto_only=False),
        _item("2027 Kia EV3 review: small but mighty", "https://a.com/5"),
        _item("Stellantis names chief", "https://www.cbtnews.com/cbt-automotive-newscast-november-13/"),
    ]
    kept, _ = S.prefilter(items, RULES)
    assert [i["title"] for i in kept] == ["Kia to invest $2 bln in India to ramp up production"]


# ---------------------------------------------------------------- Exa searches

EXA_SOURCE = {"id": "exa-acquisition", "name": "Exa", "type": "exa", "query": "dealer group acquires car dealerships", "tier": 3, "auto_only": False}
EXA_RESPONSE = {"results": [
    {"title": "Dealer Group C acquires five-store group", "url": "https://www.example-trade.com/news/dgc-acquires",
     "publishedDate": "2026-11-14T09:30:00.000Z", "highlights": ["Dealer Group C has agreed to buy five stores in Texas."]},
    {"title": "No date, so skipped", "url": "https://example.com/x"},
    {"title": "", "url": "https://example.com/y", "publishedDate": "2026-11-14T09:30:00.000Z"},
]}


def test_parse_exa_uses_publisher_link_and_article_sentences():
    items = S.parse_exa(EXA_RESPONSE, EXA_SOURCE)
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://www.example-trade.com/news/dgc-acquires"
    assert it["publisher"] == "example-trade.com"
    assert it["snippet"] == "Dealer Group C has agreed to buy five stores in Texas."
    assert it["published"] == dt.datetime(2026, 11, 14, 9, 30, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        S.parse_exa({"error": "bad key"}, EXA_SOURCE)


def test_exa_searches_skipped_without_key_and_failures_dont_stop_the_run(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    rules = fixture_rules()
    rules["sources"] = rules["sources"] + [EXA_SOURCE, {**EXA_SOURCE, "id": "exa-broken"}]
    now = fixture_now()
    since = now - dt.timedelta(hours=72)

    # No key: Exa sources aren't attempted at all, so they don't count as failures.
    _, ok, failed = S.collect(rules, since, now, fixture_fetcher)
    assert failed == [] and not any(i.startswith("exa-") for i in ok)

    # With Exa available: results come in, and one failing search is skipped like any other source.
    def fake_exa(source, start):
        assert start == since
        if source["id"] == "exa-broken":
            raise RuntimeError("timeout")
        resp = json.loads(json.dumps(EXA_RESPONSE))
        resp["results"][0]["publishedDate"] = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return resp

    items, ok, failed = S.collect(rules, since, now, fixture_fetcher, exa=fake_exa)
    assert "exa-acquisition" in ok and [f["id"] for f in failed] == ["exa-broken"]
    assert any(i["publisher"] == "example-trade.com" and not i["is_aggregator"] for i in items)


# ---------------------------------------------------------------- keyword rules

CTX =S.RulesContext(RULES, S.parse_watchlist(["Lithia Motors | Lithia", "AutoNation", "Hyundai", "Toyota"]))


def _story(title, snippet=""):
    return {"title": title, "snippet": snippet}


def test_rules_acquirer_and_target():
    t = S.classify_rules(_story("Lithia Motors acquires Smith Auto Group in Ohio"), CTX)
    assert t["signal"] == "acquisition"
    roles = {c["name"]: c["role"] for c in t["companies"]}
    assert roles["Lithia Motors"] == "acquirer" and roles["Smith Auto Group"] == "target"
    t = S.classify_rules(_story("Smith Auto Group acquired by AutoNation"), CTX)
    roles = {c["name"]: c["role"] for c in t["companies"]}
    assert roles["AutoNation"] == "acquirer" and roles["Smith Auto Group"] == "target"


def test_rules_confidence():
    assert S.classify_rules(_story("Lithia Motors acquires two Honda stores"), CTX)["confidence"] == 0.75
    assert S.classify_rules(_story("Market update", "Lithia Motors acquires two stores"), CTX)["confidence"] == 0.5


def test_rules_ignore_brand_lookalikes():
    t = S.classify_rules(_story("HD Hyundai launches autonomous vessel with Anduril"), CTX)
    assert "Hyundai" not in t["brands"]
    assert not S.match_watchlist(t, CTX_WATCH)[0] and not S.match_watchlist(t, CTX_WATCH)[1]


def test_rules_regulation_is_not_any_rule():
    assert S.classify_rules(_story("Car salesman reveals unwritten rule for selling cars"), CTX)["signal"] != "regulation"
    assert S.classify_rules(_story("FTC finalizes new rule for auto dealers"), CTX)["signal"] == "regulation"


def test_rules_sales_direction():
    assert S.classify_rules(_story("Kia sales rise 12% in October"), CTX)["direction"] == "rising"
    assert S.classify_rules(_story("Nissan sales fall 9% in October"), CTX)["direction"] == "falling"


# ---------------------------------------------------------------- routing (every cell)

CTX_WATCH = S.parse_watchlist(["AutoNation | AutoNation USA", "Kia"])


def route(**t):
    r = S.route(tag(**t), CTX_WATCH, RULES)
    return r[0] if r else None


@pytest.mark.parametrize("signal,watched_company,watched_brand,other", [
    ("closure", "potential_risk", "lower_priority", "lower_priority"),
    ("new_store", "potential_growth", "new_business", "new_business"),
    ("market_entry", "potential_growth", "new_business", "new_business"),
    ("competitor_selected", "potential_risk", "lower_priority", "lower_priority"),
    ("leadership_change", "potential_risk", "new_business", "new_business"),
    ("tech_investment", "potential_growth", "new_business", "new_business"),
    # A watched name named as the company in a brand story is a watchlist match too.
    ("oem_retail_strategy", "potential_growth", "potential_growth", "industry_development"),
    # Watched brands: launches and recalls are left out.
    ("major_recall", None, None, None),
    ("launch", None, None, "industry_development"),
    ("competitor_incident", "industry_development", "industry_development", "new_business"),
    ("regulation", "industry_development", "industry_development", "industry_development"),
])
def test_routing_table(signal, watched_company, watched_brand, other):
    assert route(signal=signal, companies=[{"name": "AutoNation", "role": "subject"}]) == watched_company
    assert route(signal=signal, brands=["Kia"]) == watched_brand
    assert route(signal=signal, companies=[{"name": "Someone Else", "role": "subject"}]) == other


def test_routing_acquisition_direction_matters():
    assert route(companies=[{"name": "AutoNation", "role": "acquirer"}]) == "potential_growth"
    assert route(companies=[{"name": "AutoNation USA", "role": "target"}]) == "potential_risk"  # alias
    assert route(companies=[{"name": "Other Co", "role": "acquirer"}]) == "new_business"


def test_routing_certification_and_sales():
    cert = lambda n: [{"name": n, "role": "certified"}]  # noqa: E731
    assert route(signal="oem_dms_certification", brands=["Kia"], companies=cert("CDK Global")) == "potential_risk"
    assert route(signal="oem_dms_certification", brands=["Kia"], companies=cert("Tekion")) == "potential_growth"
    assert route(signal="oem_dms_certification", companies=cert("Tekion")) == "new_business"
    assert route(signal="oem_dms_certification", companies=cert("Other Vendor")) == "industry_development"
    assert route(signal="sales_shift", brands=["Kia"], direction="falling") is None  # brand sales figures are left out


def test_routing_without_watchlist():
    r = S.route(tag(signal="closure", companies=[{"name": "AutoNation", "role": "subject"}]), [], RULES)
    assert r == ("lower_priority", None)
    assert S.route(tag(relevant=False, signal=None), [], RULES) is None


def test_routing_reports_matched_name():
    assert S.route(tag(signal="oem_retail_strategy", brands=["Kia"]), CTX_WATCH, RULES) == ("potential_growth", "Kia")


# ---------------------------------------------------------------- ranking

def _cand(section, conf, companies=(), tier=2, matched=None, host="x.com"):
    st = _item(f"story {conf} {section} {companies}", f"https://{host}/{section}{conf}{companies}", tier=tier)
    st["outlets"] = 1
    return {"story": st, "tag": tag(confidence=conf, companies=[{"name": c, "role": "subject"} for c in companies]),
            "section": section, "matched": matched}


def test_sections_labels_signal_order_and_caps():
    out = S.rank([_cand("new_business", 0.9, ["Dealer Group D"], host="a.com"),
                  _cand("industry_development", 0.95, host="b.com")], RULES)
    # Background stories are left out; only prospects go to New accounts.
    assert [c["lead"] for c in out["new_accounts"]] == ["Dealer Group D"] and out["existing_accounts"] == []
    # Watchlist stories go to Existing accounts, labelled company · region, ordered by sales signal.
    an = _cand("potential_risk", 0.9, matched="AutoNation")
    an["tag"].update(region="North America", signal="closure")
    kia = _cand("potential_growth", 0.4, matched="Kia", host="a.com")
    kia["tag"]["signal"] = "competitor_selected"
    ford = _cand("potential_risk", 0.95, matched="Ford", host="c.com")
    ford["tag"]["signal"] = "major_recall"
    out = S.rank([an, kia, ford], RULES)
    # A competitor win comes before a closure, which comes before a recall, whatever the confidence.
    assert [c["lead"] for c in out["existing_accounts"]] == ["Kia", "AutoNation · North America", "Ford"]
    many = [_cand("new_business", 0.9 - i / 100, host=f"h{i}.com") for i in range(9)]
    assert len(S.rank(many, RULES)["new_accounts"]) == 6


def test_tekion_and_competitor_news_has_its_own_section():
    def vendor(name, signal, conf=0.9, host="v.com"):
        c = _cand("industry_development", conf, host=host)
        c["tag"] = tag(signal=signal, confidence=conf, companies=[{"name": name, "role": "subject"}])
        return c
    cands = [vendor("CDK", "competitor_incident"), vendor("Tekion", "vendor_news", host="w.com"),
             vendor("Reynolds and Reynolds", "competitor_selected", host="y.com")]
    out = S.rank(cands, RULES)
    leads = [c["lead"] for c in out["tekion_competitors"]]
    assert leads[0] == "Tekion"  # our own news first
    assert set(leads[1:]) == {"CDK Global", "Reynolds and Reynolds"}
    assert out["new_accounts"] == [] and out["existing_accounts"] == []
    # From any region: a competitor's news abroad still counts.
    t_ = tag(signal="vendor_news", companies=[{"name": "Keyloop", "role": "subject"}], region="Europe")
    assert S.route(t_, CTX_WATCH, RULES, ["North America"]) is not None


def test_at_most_two_items_per_outlet():
    same = [_cand("new_business", 0.9 - i / 100, host="theedgemalaysia.com") for i in range(5)]
    assert len(S.rank(same, RULES)["new_accounts"]) == 2


def test_nothing_is_marked_priority():
    out = S.rank([_cand("new_business", 0.8, ["Busy Group"]), _cand("new_business", 0.9, ["Busy Group"], host="a.com")], RULES)
    assert all("priority" not in c for c in out["new_accounts"])


def test_passing_mention_is_not_a_watchlist_match():
    t_ = tag(signal="acquisition", companies=[{"name": "Maroone Group", "role": "acquirer"}, {"name": "AutoNation", "role": "other"}])
    assert S.route(t_, CTX_WATCH, RULES) == ("new_business", None)


def test_regions():
    abroad = dict(region="Asia Pacific")
    # Prospects and watched-brand news outside the team's regions are left out...
    assert S.route(tag(signal="acquisition", companies=[{"name": "Sime Darby", "role": "acquirer"}], **abroad), CTX_WATCH, RULES, ["North America"]) is None
    assert S.route(tag(signal="launch", brands=["Kia"], **abroad), CTX_WATCH, RULES, ["North America"]) is None
    # ...but a watched company is an account wherever the news happens, and competitor incidents always count.
    assert S.route(tag(signal="acquisition", companies=[{"name": "AutoNation", "role": "target"}], **abroad), CTX_WATCH, RULES, ["North America"]) == ("potential_risk", "AutoNation")
    assert S.route(tag(signal="competitor_incident", companies=[{"name": "CDK Global", "role": "subject"}], **abroad), CTX_WATCH, RULES, ["North America"])
    # In region, Global, Unknown or no regions configured: normal routing.
    for r in ("North America", "Global", "Unknown"):
        assert S.route(tag(signal="acquisition", companies=[{"name": "X Group", "role": "acquirer"}], region=r), CTX_WATCH, RULES, ["North America"])
    assert S.route(tag(signal="acquisition", companies=[{"name": "X Group", "role": "acquirer"}], **abroad), CTX_WATCH, RULES)


def test_oem_moving_away_from_dealers_is_a_risk():
    assert S.route(tag(signal="oem_retail_strategy", brands=["Kia"], direction="falling"), CTX_WATCH, RULES)[0] == "potential_risk"
    assert S.route(tag(signal="oem_retail_strategy", brands=["Kia"], direction="rising"), CTX_WATCH, RULES)[0] == "potential_growth"


def test_url_date_older_than_reported_date():
    now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
    assert S.url_date_is_older("https://site.com/2023/05/14/autonation-plan", now)
    assert S.url_date_is_older("https://site.com/news/2024-02-01-story", now)
    assert not S.url_date_is_older("https://site.com/2026/11/14/story", now)
    assert not S.url_date_is_older("https://www.floridatrend.com/article/26128/x", now)


# ---------------------------------------------------------------- AI fallback

def test_ai_failure_falls_back_to_rules_for_that_batch_only(monkeypatch):
    stories = [dict(_item(f"Lithia Motors acquires store number {i}", f"https://x.com/{i}"), publishers=set(), urls=set()) for i in range(45)]
    calls = []

    def fake_batch(client, rules, batch):
        calls.append(batch[0]["id"])
        if batch[0]["id"] == "20":
            raise S.BatchFailed("forced")
        return {s["id"]: tag(companies=[{"name": "Lithia Motors", "role": "acquirer"}]) for s in batch}, []

    monkeypatch.setattr(S, "ai_tag_batch", fake_batch)
    tags, fallback = S.classify(stories, RULES, CTX, client=object())
    assert fallback == 1
    assert len(calls) == 4  # three batches, the failing one retried once
    assert sum(t["tagged_by"] == "rules" for t in tags.values()) == 20
    assert sum(t["tagged_by"] == "ai" for t in tags.values()) == 25


# ---------------------------------------------------------------- whole run (scheduling, sending, state)

def _write_config(tmp_path, watchlist=True):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("recipients: [team@example.com, rep@example.com]\nsend_hour: 8\ntimezone: America/Chicago\ncontact: Shushrut\n"
                   + ("watchlist:\n  - Lithia Motors | Lithia\n  - Toyota\n" if watchlist else ""))
    rules = tmp_path / "rules.yaml"
    import yaml
    raw = yaml.safe_load((ROOT / "rules.yaml").read_text(encoding="utf-8"))
    raw["sources"] = [{k: v for k, v in s.items() if k != "file"} for s in FIXTURE_SOURCES]
    rules.write_text(yaml.safe_dump(raw))
    return cfg, rules


class Outbox:
    def __init__(self, fail_times=0):
        self.sent, self.attempts, self.fail_times = [], 0, fail_times

    def __call__(self, recipients, subject, html_body, text_body):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("SMTP down (test)")
        self.sent.append({"to": recipients, "subject": subject, "html": html_body, "text": text_body})


def _at_local(hour: int) -> dt.datetime:
    base = fixture_now().astimezone(ZoneInfo("America/Chicago"))
    day = base.date() + dt.timedelta(days=1 if base.hour >= hour else 0)
    return dt.datetime(day.year, day.month, day.day, hour, 17, tzinfo=ZoneInfo("America/Chicago")).astimezone(dt.timezone.utc)


def _run(tmp_path, now, outbox, *extra, watchlist=True):
    cfg, rules = _write_config(tmp_path, watchlist)
    args = ["--config", str(cfg), "--rules", str(rules), "--state", str(tmp_path / "sent.json"), "--out", str(tmp_path / "out"), *extra]
    return S.main(args, now=now, fetcher=fixture_fetcher, ai_client=False, sender=outbox)


def test_not_due_before_send_hour(tmp_path):
    box = Outbox()
    assert _run(tmp_path, _at_local(7), box) == 0
    assert box.attempts == 0


def test_sends_once_per_day(tmp_path):
    box = Outbox()
    at8 = _at_local(8)
    assert _run(tmp_path, at8, box) == 0
    assert len(box.sent) == 1 and box.sent[0]["to"] == ["team@example.com", "rep@example.com"]
    _run(tmp_path, at8 + dt.timedelta(minutes=20), box)  # duplicate trigger
    _run(tmp_path, at8 + dt.timedelta(hours=1), box)
    assert len(box.sent) == 1
    state = json.loads((tmp_path / "sent.json").read_text(encoding="utf-8"))
    assert state["last_sent_date"] and state["runs"][-1]["status"] in ("ok", "degraded")


def test_failed_send_retries_then_stops(tmp_path):
    box = Outbox(fail_times=2)
    at8 = _at_local(8)
    assert _run(tmp_path, at8, box) == 1  # fails → job fails → GitHub alerts the owner
    assert _run(tmp_path, at8 + dt.timedelta(hours=1), box) == 1
    assert _run(tmp_path, at8 + dt.timedelta(hours=2), box) == 0  # 10:17, succeeds
    assert len(box.sent) == 1
    box2 = Outbox(fail_times=99)
    p = tmp_path / "late"
    p.mkdir()
    _run(p, at8, box2)
    _run(p, at8 + dt.timedelta(hours=3), box2)  # 11:17: outside the retry window
    assert box2.attempts == 1


def test_preview_sends_nothing_and_writes_files(tmp_path):
    box = Outbox()
    assert _run(tmp_path, fixture_now(), box, "--preview") == 0
    assert box.attempts == 0
    html_out = (tmp_path / "out" / "preview.html").read_text(encoding="utf-8")
    text_out = (tmp_path / "out" / "preview.txt").read_text(encoding="utf-8")
    assert "Hi Team," in html_out and text_out.startswith("Subject: Automotive market update – ")
    assert "Here's what happened in the automotive world in the last 24 hours." in text_out and "NEW ACCOUNTS" in text_out
    assert not (tmp_path / "sent.json").exists()  # previews don't touch state


def test_test_copy_goes_to_one_address_and_does_not_count(tmp_path):
    box = Outbox()
    assert _run(tmp_path, _at_local(8), box, "--send-now", "--to", "me@example.com") == 0
    assert box.sent[0]["to"] == ["me@example.com"]
    assert json.loads((tmp_path / "sent.json").read_text(encoding="utf-8"))["last_sent_date"] is None


def test_email_format(tmp_path):
    box = Outbox()
    _run(tmp_path, _at_local(8), box, watchlist=False)
    mail = box.sent[0]
    assert mail["subject"].startswith("Automotive market update – ")
    assert mail["text"].startswith("Hi Team,\n\nHere's what happened in the automotive world in the last 24 hours.")
    assert "NEW ACCOUNTS" in mail["text"]
    assert "Existing accounts" not in mail["html"]  # no watchlist, so no existing-accounts section
    assert "This is an automated email, so replies aren’t read. To share feedback, message Shushrut" in mail["text"] and "message Shushrut on Slack or by email" in mail["text"]
    assert " should " not in mail["text"].lower()
    import re
    urls = re.findall(r"\(([^:()]+): (https?://\S+)\)", mail["text"])
    assert len({u for _, u in urls}) == len(urls)  # no duplicate stories


def test_next_day_skips_what_was_already_sent(tmp_path):
    box = Outbox()
    at8 = _at_local(8)
    _run(tmp_path, at8, box)
    first = set(re_urls(box.sent[0]["text"]))
    _run(tmp_path, at8 + dt.timedelta(days=1), box)
    second = set(re_urls(box.sent[1]["text"]))
    assert not (first & second)


def re_urls(text):
    import re
    return re.findall(r": (https?://\S+)\)", text)


def test_ai_response_parsing_and_name_check():
    """Exercises ai_tag_batch against a response shaped like the real API's."""
    from types import SimpleNamespace as NS

    batch = [dict(_item("Lithia Motors acquires two Texas stores", "https://x.com/a"), id="0"),
             dict(_item("Dealer group opens new store", "https://x.com/b"), id="1"),
             dict(_item("Review of the week", "https://x.com/c"), id="2")]
    results = [
        {"id": "0", "relevant": True, "signal": "acquisition", "companies": [{"name": "Lithia Motors", "role": "acquirer"}], "brands": [],
         "direction": None, "summary": "Lithia Motors acquired two stores in Texas.", "implication": "Ownership changes typically prompt a systems review.", "confidence": 0.92},
        # Invents a company that isn't in the text → must fall back to rules.
        {"id": "1", "relevant": True, "signal": "new_store", "companies": [{"name": "Made Up Motors", "role": "subject"}], "brands": [],
         "direction": None, "summary": "x", "implication": "You should reach out.", "confidence": 0.8},
        # Item 2 omitted by the model → also falls back.
    ]

    class FakeMessages:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"results": results}))])

    client = NS(messages=FakeMessages())
    tags, rejected = S.ai_tag_batch(client, RULES, batch)
    assert set(tags) == {"0"} and sorted(rejected) == ["1", "2"]
    assert tags["0"]["companies"] == [{"name": "Lithia Motors", "role": "acquirer"}] and tags["0"]["tagged_by"] == "ai"
    sent = client.messages.kwargs
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_ai_refusal_counts_as_batch_failure():
    from types import SimpleNamespace as NS

    client = NS(messages=NS(create=lambda **k: NS(stop_reason="refusal", content=[])))
    with pytest.raises(S.BatchFailed):
        S.ai_tag_batch(client, RULES, [dict(_item("x", "https://x.com/z"), id="0")])
