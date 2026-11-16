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
    assert S.load_config(ok)["regions"] == []  # regions are optional


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

CTX = S.RulesContext(RULES)


def _story(title, snippet=""):
    return {"title": title, "snippet": snippet}


def test_rules_acquirer_and_target():
    t = S.classify_rules(_story("Lithia Motors acquires Smith Auto Group in Ohio"), CTX)
    assert t["signal"] == "acquisition"
    roles = {c["name"]: c["role"] for c in t["companies"]}
    assert roles["Lithia Motors"] == "acquirer" and roles["Smith Auto Group"] == "target"
    t = S.classify_rules(_story("Smith Auto Group acquired by Penske Automotive Group"), CTX)
    roles = {c["name"]: c["role"] for c in t["companies"]}
    assert roles["Penske Automotive Group"] == "acquirer" and roles["Smith Auto Group"] == "target"


def test_rules_confidence():
    assert S.classify_rules(_story("Lithia Motors acquires two Honda stores"), CTX)["confidence"] == 0.75
    assert S.classify_rules(_story("Market update", "Lithia Motors acquires two stores"), CTX)["confidence"] == 0.5


def test_rules_ignore_brand_lookalikes():
    t = S.classify_rules(_story("HD Hyundai launches autonomous vessel with Anduril"), CTX)
    assert "Hyundai" not in t["brands"]


def test_rules_regulation_is_not_any_rule():
    assert S.classify_rules(_story("Car salesman reveals unwritten rule for selling cars"), CTX)["signal"] != "regulation"
    assert S.classify_rules(_story("FTC finalizes new rule for auto dealers"), CTX)["signal"] == "regulation"


def test_rules_sales_direction():
    assert S.classify_rules(_story("Kia sales rise 12% in October"), CTX)["direction"] == "rising"
    assert S.classify_rules(_story("Nissan sales fall 9% in October"), CTX)["direction"] == "falling"


# ---------------------------------------------------------------- routing

def route(regions=None, **t):
    return S.route(tag(**t), RULES, regions)


@pytest.mark.parametrize("signal,section", [
    ("acquisition", "account_news"), ("closure", "account_news"), ("new_store", "account_news"),
    ("market_entry", "account_news"), ("leadership_change", "account_news"), ("tech_investment", "account_news"),
    ("oem_retail_strategy", "industry_trends"), ("regulation", "industry_trends"), ("market_trend", "industry_trends"),
    ("sales_shift", "industry_trends"), ("launch", "industry_trends"), ("oem_dms_certification", "industry_trends"),
    ("major_recall", None),
    # Without us or a competitor named: news about other dealer software companies is a trend.
    ("competitor_incident", "industry_trends"), ("vendor_news", "industry_trends"),
])
def test_routing_table(signal, section):
    assert route(signal=signal, companies=[{"name": "Smith Auto Group", "role": "subject"}]) == section


def test_routing_us_and_competitors():
    assert route(signal="vendor_news", companies=[{"name": "Tekion", "role": "subject"}]) == "tekion"
    assert route(signal="competitor_incident", companies=[{"name": "CDK", "role": "subject"}]) == "competitors"
    assert route(signal="oem_dms_certification", companies=[{"name": "Tekion", "role": "certified"}]) == "tekion"
    assert route(signal="oem_dms_certification", companies=[{"name": "CDK Global", "role": "certified"}]) == "competitors"
    # A dealer choosing a competitor is account news about the dealer, and needs a named dealer.
    win = [{"name": "Smith Auto Group", "role": "subject"}, {"name": "Reynolds and Reynolds", "role": "subject"}]
    assert route(signal="competitor_selected", companies=win) == "account_news"
    assert route(signal="competitor_selected", companies=win[1:]) is None
    assert route(relevant=False, signal=None) is None


def test_account_news_needs_a_buyer():
    # An insurer buying a warranty company isn't a business that buys dealer software: an industry trend instead.
    assert route(signal="acquisition", buyer=False, companies=[{"name": "Arch Insurance", "role": "acquirer"}]) == "industry_trends"
    assert route(signal="tech_investment", buyer=False, companies=[{"name": "Toma", "role": "subject"}]) == "industry_trends"
    assert route(signal="acquisition", buyer=True, companies=[{"name": "Tom Wood Group", "role": "acquirer"}]) == "account_news"
    assert route(signal="acquisition", buyer=None, companies=[{"name": "Tom Wood Group", "role": "acquirer"}]) == "account_news"  # rules: unknown
    # Trends don't need a buyer.
    assert route(signal="regulation", buyer=False) == "industry_trends"


def test_regions():
    na = ["North America"]
    assert route(na, signal="acquisition", region="Asia Pacific") is None
    assert route(na, signal="launch", region="Europe") is None
    for r in ("North America", "Global", "Unknown"):
        assert route(na, signal="acquisition", region=r) == "account_news"
    assert route(None, signal="acquisition", region="Asia Pacific") == "account_news"  # no regions set: everything
    # Competitor and Tekion news counts from anywhere, but a foreign dealer choosing a competitor doesn't.
    assert route(na, signal="vendor_news", region="Europe", companies=[{"name": "Keyloop", "role": "subject"}]) == "competitors"
    far = [{"name": "Euro Motors", "role": "subject"}, {"name": "Keyloop", "role": "subject"}]
    assert route(na, signal="competitor_selected", region="Europe", companies=far) is None


# ---------------------------------------------------------------- ranking

# The limits are off by default (every story is sent); these tests switch them on.
LIMITED = {**RULES, "settings": {**RULES["settings"], "max_per_outlet": 2, "trends_one_per_type": True, "hot_news_gate": 0.6,
                                 "caps": {"industry_trends": 3, "account_news": 8, "competitors": 5, "tekion": 3}}}


def test_limits_are_off_by_default():
    s = RULES["settings"]
    assert not s.get("caps") and not s.get("max_per_outlet") and not s.get("trends_one_per_type") and s["sent_memory_days"] == 0
    assert not s.get("hot_news_gate")  # no confidence filter


def _cand(section, conf, companies=(), tier=2, host="x.com", signal="acquisition"):
    st = _item(f"story {conf} {section} {companies} {host}", f"https://{host}/{section}{conf}{companies}", tier=tier)
    st["outlets"] = 1
    return {"story": st, "tag": tag(confidence=conf, signal=signal, companies=[{"name": c, "role": "subject"} for c in companies]),
            "section": section}


def test_section_order_labels_signal_order_and_caps():
    assert S.SECTIONS == ["industry_trends", "account_news", "competitors", "tekion"]
    a = _cand("account_news", 0.9, ["AutoNation"], signal="closure")
    a["tag"]["region"] = "North America"
    b = _cand("account_news", 0.7, ["Kia"], host="a.com", signal="new_store")
    c = _cand("account_news", 0.95, ["Ford"], host="c.com", signal="leadership_change")
    out = S.rank([a, b, c], RULES)
    # Ordered by sales signal, whatever the confidence; labelled company · region.
    assert [x["lead"] for x in out["account_news"]] == ["Kia", "Ford", "AutoNation · North America"]
    many = [_cand("account_news", 0.9 - i / 100, host=f"h{i}.com") for i in range(12)]
    assert len(S.rank(many, RULES)["account_news"]) == 12  # no caps by default
    assert len(S.rank(many, LIMITED)["account_news"]) == 8


def test_competitors_and_tekion_have_their_own_sections():
    def vendor(name, signal, section, host):
        c = _cand(section, 0.9, host=host, signal=signal)
        c["tag"]["companies"] = [{"name": name, "role": "subject"}]
        return c
    win = vendor("Reynolds and Reynolds", "competitor_selected", "account_news", "y.com")
    win["tag"]["companies"].insert(0, {"name": "Smith Auto Group", "role": "subject"})
    out = S.rank([vendor("CDK", "competitor_incident", "competitors", "v.com"),
                  vendor("Tekion", "vendor_news", "tekion", "w.com"), win], RULES)
    assert [c["lead"] for c in out["competitors"]] == ["CDK Global"]
    assert [c["lead"] for c in out["tekion"]] == ["Tekion"]
    assert [c["lead"] for c in out["account_news"]] == ["Smith Auto Group"]  # labelled with the dealer


def test_at_most_two_items_per_outlet():
    same = [_cand("account_news", 0.9 - i / 100, host="theedgemalaysia.com") for i in range(5)]
    assert len(S.rank(same, LIMITED)["account_news"]) == 2
    assert len(S.rank(same, RULES)["account_news"]) == 5


def test_nothing_is_marked_priority():
    out = S.rank([_cand("account_news", 0.8, ["Busy Group"]), _cand("account_news", 0.9, ["Busy Group"], host="a.com")], RULES)
    assert all("priority" not in c for c in out["account_news"])


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

def _write_config(tmp_path, memory_days=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("recipients: [team@example.com, rep@example.com]\nsend_hour: 8\ntimezone: America/Chicago\ncontact: Shushrut\n")
    rules = tmp_path / "rules.yaml"
    import yaml
    raw = yaml.safe_load((ROOT / "rules.yaml").read_text(encoding="utf-8"))
    raw["sources"] = [{k: v for k, v in s.items() if k != "file"} for s in FIXTURE_SOURCES]
    raw["settings"]["hot_news_gate"] = 0.5  # the saved fixture day only has keyword-rule tags (confidence 0.5)
    if memory_days is not None:
        raw["settings"]["sent_memory_days"] = memory_days
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


def _run(tmp_path, now, outbox, *extra, memory_days=14):
    cfg, rules = _write_config(tmp_path, memory_days)
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
    assert "Here's what happened in the automotive world in the last 24 hours." in text_out and "ACCOUNT NEWS" in text_out
    assert not (tmp_path / "sent.json").exists()  # previews don't touch state


def test_test_copy_goes_to_one_address_and_does_not_count(tmp_path):
    box = Outbox()
    assert _run(tmp_path, _at_local(8), box, "--send-now", "--to", "me@example.com") == 0
    assert box.sent[0]["to"] == ["me@example.com"]
    assert json.loads((tmp_path / "sent.json").read_text(encoding="utf-8"))["last_sent_date"] is None


def test_email_format(tmp_path):
    box = Outbox()
    _run(tmp_path, _at_local(8), box)
    mail = box.sent[0]
    assert mail["subject"].startswith("Automotive market update – ")
    assert mail["text"].startswith("Hi Team,\n\nHere's what happened in the automotive world in the last 24 hours.")
    assert "ACCOUNT NEWS" in mail["text"]
    assert "Why it matters" not in mail["text"]  # keyword rules never add a stock line
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


def test_prompt_offers_products_but_keeps_why_it_matters_optional():
    p = S.system_prompt(RULES)
    assert "Automotive Retail Cloud" in p and "Leave it empty only when the line would be generic" in p
    assert "buyer:" in p and "Political statements" in p and "Routine hires below the C-suite" in p


def test_industry_trends_first_one_per_type_capped_and_gated():
    def trend(signal, conf=0.9, host="t.com"):
        return _cand("industry_trends", conf, host=host, signal=signal)
    cands = [trend("launch", host="a.com"), trend("launch", 0.95, host="b.com"), trend("regulation", host="c.com"),
             trend("market_trend", host="d.com"), trend("oem_retail_strategy", host="e.com"), trend("sales_shift", host="f.com")]
    out = S.rank(cands, LIMITED)
    sigs = [c["tag"]["signal"] for c in out["industry_trends"]]
    assert sigs == ["oem_retail_strategy", "regulation", "market_trend"]  # signal order, one per type, at most 3
    assert S.SECTIONS[0] == "industry_trends"
    # Not hot news (general knowledge, explainer, opinion): left out of every section.
    assert S.rank([trend("regulation", 0.4)], LIMITED)["industry_trends"] == []
    assert S.rank([_cand("account_news", 0.4, ["Dealer Group D"])], LIMITED)["account_news"] == []
    assert len(S.rank([trend("regulation", 0.2)], RULES)["industry_trends"]) == 1  # gate off by default


def test_a_day_with_only_trends_is_not_a_quiet_day():
    import datetime as dt
    c = _cand("industry_trends", 0.9, signal="regulation")
    c["lead"] = "FTC · North America"
    secs = {s: [] for s in S.SECTIONS}
    secs["industry_trends"] = [c]
    now = dt.datetime(2026, 11, 16, 8, tzinfo=dt.timezone.utc)
    _, _, text = S.compose(secs, now, None, False, "Shushrut")
    assert "Here's what happened" in text and "INDUSTRY TRENDS" in text and "Nothing relevant" not in text


def test_trusted_outlets_rank_like_tier_one():
    def it(url, publisher, search=True):
        return {"url": url, "publisher": publisher, "tier": 3, "from_search": search}
    items = S.rate_outlets([it("https://www.reuters.com/x", "reuters.com"), it("https://news.google.com/a", "Automotive News"),
                            it("https://hoodline.com/y", "hoodline.com"), it("https://feed.example.com/z", "Feed", search=False)], RULES)
    assert [i["tier"] for i in items] == [1, 1, 3, 3]  # a feed keeps its own tier


def test_same_day_send_does_not_count_as_already_sent(tmp_path):
    box = Outbox()
    assert _run(tmp_path, _at_local(8), box, memory_days=14) == 0
    path = tmp_path / "sent.json"
    assert json.loads(path.read_text(encoding="utf-8"))["sent_items"]
    # A second send the same day (send-now to the team) still has today's stories.
    assert _run(tmp_path, _at_local(8), box, "--send-now", memory_days=14) == 0
    assert "ACCOUNT NEWS" in box.sent[-1]["text"]
    # Stories sent on an earlier day are still left out.
    state = json.loads(path.read_text(encoding="utf-8"))
    for x in state["sent_items"]:
        x["at"] = (dt.datetime.fromisoformat(x["at"]) - dt.timedelta(days=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    assert _run(tmp_path, _at_local(8), box, "--send-now", memory_days=14) == 0
    assert "ACCOUNT NEWS" not in box.sent[-1]["text"]
    # With the memory off (the current setting), they're sent again.
    assert _run(tmp_path, _at_local(8), box, "--send-now", memory_days=0) == 0
    assert "ACCOUNT NEWS" in box.sent[-1]["text"]
