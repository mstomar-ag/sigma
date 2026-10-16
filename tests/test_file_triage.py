"""Slice 14 of the rulebook: the pure file triage core."""
import ast
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "skills" / "sigma-loop" / "scripts" / "file_triage.py"


def _mod():
    spec = importlib.util.spec_from_file_location("file_triage", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(**over):
    block = {"triage": {"enabled": True}, "buckets": {"urgent": "P2", "routine": "P3", "unclear": "P4"}}
    block.update(over)
    return {"ai_filed": block}


def test_bucket_at_or_above_human_default_closes():
    m = _mod()
    for bad in ("P1", "P0"):
        cfg = _cfg(buckets={"urgent": bad, "routine": "P3", "unclear": "P4"})
        v = m.validate_buckets(cfg)
        assert v["ok"] is False and "urgent" in v["reason"]
        d = m.decide("tidy a comment", "cleanup", {}, cfg)
        assert d.kind == "park" and "urgent" in d.reason
    assert m.validate_buckets(_cfg())["ok"] is True


def test_hard_stop_word_parks_never_arms():
    m = _mod()
    for text in ("run rm -rf on the cache", "this is irreversible", "drop table users"):
        d = m.decide("cleanup", text, {}, _cfg())
        assert d.kind == "park", text
    assert m.decide("cleanup", "tidy a comment", {}, _cfg()).kind == "arm"
    assert m.decide("cleanup", "rm -rf x", {"human_confirmed": True, "priority": "P1"}, _cfg()).kind == "park"


def test_marker_first_line_parks():
    m = _mod()
    marker = "sigma:spend-" + "approved=lab1"
    assert m.decide("cleanup", marker + "\nbody", {}, _cfg()).kind == "park"
    assert m.decide("cleanup", "body\n" + marker, {}, _cfg()).kind == "arm"


def test_never_returns_blocking():
    m = _mod()
    texts = ["", "exploit found", "cosmetic", "cleanup", "every run degrades", None, 5]
    for t in texts:
        for flags in ({}, {"bucket": "urgent"}, {"human_confirmed": True, "priority": "blocking"}, None):
            d = m.decide(t, t, flags, _cfg())
            assert "block" not in str(d).lower()
            assert d.kind in ("arm", "park", "queue")
    assert m.validate_buckets(_cfg(buckets={"urgent": "blocking", "routine": "P3", "unclear": "P4"}))["ok"] is False


def test_triage_off_returns_queue():
    m = _mod()
    d = m.decide("cleanup", "x", {}, _cfg(triage={"enabled": False}))
    assert d.kind == "queue" and d.priority is None
    assert m.decide("cleanup", "x", {}, {}).kind == "arm"  # absent block: on by default
    assert m.decide("cleanup", "x", {}, _cfg(triage={"enabled": "no"})).kind == "park"


def test_decision_is_login_free():
    m = _mod()
    a = m.decide("exploit in parser", "b", {"login": "one"}, _cfg())
    b = m.decide("exploit in parser", "b", {"login": "two"}, _cfg())
    assert a == b and a.kind == "arm" and a.priority == "P2"
    assert "login" not in SRC.read_text(encoding="utf-8").replace("login-free", "")


def test_human_confirmed_keeps_plan_priority():
    m = _mod()
    d = m.decide("t", "b", {"human_confirmed": True, "priority": "P1"}, _cfg())
    assert (d.kind, d.priority) == ("arm", "P1")
    assert m.decide("t", "b", {"human_confirmed": True, "priority": "high"}, _cfg()).kind == "park"


def test_pure_no_process_or_network_imports():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {n.name.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import) for n in x.names}
    names |= {x.module.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.ImportFrom) and x.module}
    assert not names & {"subprocess", "socket", "urllib", "http", "requests", "os"}


def test_template_defaults_match_code():
    m = _mod()
    tmpl = json.loads((ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text(encoding="utf-8"))
    assert "_ai_filed" in tmpl
    block = tmpl["ai_filed"]
    assert block["triage"]["enabled"] is True
    assert block["buckets"] == m.DEFAULT_BUCKETS
    assert m.validate_buckets(tmpl)["ok"] is True


_PROSE_BODIES = (
    "Found while building slice 15. It's a follow-up: the user's \"quoted\" text isn't a command. cleanup.",
    "Hand-off from the engine goal: don't re-run the doctor; it's done. The plan's step 3 is open.",
    "Decompose child: split the parser's loop. Done when the 'edge case' doesn't recur. Unbalanced \" quote.",
    "Design child: the writer's contract isn't stated; one apostrophe ' alone. follow-up polish.",
)


def test_ordinary_prose_with_odd_quotes_arms_not_parks():
    m = _mod()
    for body in _PROSE_BODIES:
        d = m.decide("Tidy the writer's docs", body, {}, _cfg())
        assert d.kind == "arm" and d.priority in ("P2", "P3", "P4"), (body, d)


def test_dangerous_command_in_prose_with_odd_apostrophe_still_parks():
    m = _mod()
    for cmd in ("git push --force origin main", "kubectl delete pod web", "git reset --hard HEAD~3"):
        d = m.decide("cleanup", "It's odd, don't run: " + cmd, {}, _cfg())
        assert d.kind == "park", cmd


def test_non_parse_cannot_tell_still_parks(monkeypatch):
    m = _mod()
    real = m._load

    def fake(name):
        if name != "hard_stop":
            return real(name)

        class H:
            @staticmethod
            def classify(text, ctx, cfg):
                return real("hard_stop").Result("cannot-tell", "detector-error", "")
        return H
    monkeypatch.setattr(m, "_load", fake)
    assert m.decide("cleanup", "plain words", {}, _cfg()).kind == "park"


SPEND_DEFAULTS = ("paid run", "rent a GPU", "new vendor", "paid API", "cloud GPU benchmark")


def test_each_default_spend_phrase_parks_naming_it():
    m = _mod()
    for ph in SPEND_DEFAULTS:
        for text in ("we should do a %s here" % ph, ("A " + ph.upper() + ".")):
            d = m.decide("cleanup", text, {}, _cfg())
            assert d.kind == "park" and ph.lower() in d.reason.lower(), (ph, d)


def test_spend_phrase_in_title_parks_even_human_confirmed():
    m = _mod()
    d = m.decide("Try a paid API", "tidy", {"human_confirmed": True, "priority": "P1"}, _cfg())
    assert d.kind == "park"


def test_spend_phrases_configured_list_overrides_and_empty_disables():
    m = _mod()
    cfg = _cfg(triage={"enabled": True, "spend_phrases": ["buy a license"]})
    assert m.decide("cleanup", "buy a License please", {}, cfg).kind == "park"
    assert m.decide("cleanup", "do a paid run", {}, cfg).kind == "arm"
    off = _cfg(triage={"enabled": True, "spend_phrases": []})
    assert m.decide("cleanup", "do a paid run on a new vendor", {}, off).kind == "arm"
    bad = _cfg(triage={"enabled": True, "spend_phrases": "paid run"})
    assert m.decide("cleanup", "do a paid run", {}, bad).kind == "park"


def test_spend_phrase_with_recorded_marker_does_not_arm_by_phrase_path():
    # PROVISIONAL: the existing first-line marker rule is unchanged (a marker parks), so the phrase check
    # never turns a marker issue into an arm.
    m = _mod()
    marker = "sigma:spend-" + "approved=lab1"
    d = m.decide("cleanup", marker + "\nuse a paid run", {}, _cfg())
    assert d.kind == "park" and "marker" in d.reason


def test_near_misses_and_normal_followups_still_arm():
    m = _mod()
    for text in ("he paid attention to the prepaid runner", "an unpaid runtime note", "tidy a comment",
                 "a rental agreement, the vendor list"):
        assert m.decide("cleanup", text, {}, _cfg()).kind == "arm", text


def test_triage_off_unchanged_for_spend_text():
    m = _mod()
    d = m.decide("x", "a paid run", {}, _cfg(triage={"enabled": False}))
    assert d.kind == "queue"


def test_money_amount_for_compute_parks_as_spend():
    m = _mod()
    for text in ("We will spend $40 on GPU time for the benchmark.", "Budget forty dollars of cloud credits.",
                 "rent an instance, 40 dollars", "use 1,200 USD of compute for this"):
        d = m.decide("cleanup", text, {}, _cfg())
        assert d.kind == "park" and "spend phrase" in d.reason, (text, d)
    off = _cfg(triage={"enabled": True, "spend_phrases": []})
    assert m.decide("cleanup", "spend $40 on GPU time", {}, off).kind == "arm"


def test_money_amount_near_misses_still_arm():
    m = _mod()
    for text in ("The price is $40 in the invoice template.", "Fix the GPU docs. Costs 5 pages.",
                 "see log:\n```\ncost $40 gpu run\n```\ntidy the docs", "a run of 40 tests, no dollars"):
        assert m.decide("cleanup", text, {}, _cfg()).kind == "arm", text


def test_euro_and_pound_amounts_for_compute_park_as_spend():
    m = _mod()
    for text in ("pay 100 euros for compute", "Spend about 50 EUR on cloud credits",
                 "Budget: \u00a340 of GPU time for the benchmark", "use 40 pounds of GPU credits",
                 "rent a node for \u20ac30", "twenty pounds sterling of compute"):
        d = m.decide("tidy docs", text, {}, _cfg())
        assert d.kind == "park" and "spend phrase" in d.reason, (text, d)


def test_euro_and_pound_near_misses_still_arm():
    m = _mod()
    for text in ("The recipe needs 40 pounds of flour.", "Show a euro sign in the docs.",
                 "see log:\n```\ncost \u00a340 gpu run\n```\ntidy the docs", "The price is \u20ac40 in the invoice template."):
        assert m.decide("tidy docs", text, {}, _cfg()).kind == "arm", text


DESCRIPTIVE = (
    "Redact SSH private keys at runtime so they never reach the log.",
    "Wire the check so the gate gets production data only through the reader.",
    "Irreversible actions are read by code and listed in the report.",
    "Document what the one-command uninstall does when it would delete all caches.",
    "Detect a force push in the hook and warn the author.",
)
ACTIONS = (
    "drop table users in production",
    "rotate the deploy credentials now",
    "force push to main after the rebase",
    "Delete all the old branches.",
)


def test_descriptive_mentions_of_deny_words_arm():
    m = _mod()
    for text in DESCRIPTIVE:
        d = m.decide("tidy the docs", text, {}, _cfg())
        assert d.kind == "arm", (text, d)


def test_action_sentences_still_park_and_name_the_sentence():
    m = _mod()
    for text in ACTIONS:
        d = m.decide("tidy the docs", text, {}, _cfg())
        assert d.kind == "park" and d.matched == "hard-stop word", (text, d)
        assert "(in: " in d.reason and text.split()[0].lower() in d.reason.lower()


def test_secret_and_spend_stay_hard_even_in_a_descriptive_sentence():
    m = _mod()
    d = m.decide("x", "Redact it: token = abcdef123456789", {}, _cfg())
    assert d.kind == "park" and d.matched == "secret"
    d = m.decide("x", "Document the plan; spend $40 on GPU time", {}, _cfg())
    assert d.kind == "park" and d.matched == "spend phrase"


def test_one_descriptive_one_action_sentence_parks():
    m = _mod()
    d = m.decide("x", "Redact private keys in logs. Then drop table users.", {}, _cfg())
    assert d.kind == "park" and "drop table" in d.reason


def test_chained_imperatives_and_quotes_follow_the_structural_rule():
    m = _mod()
    for text in ("Scrub the logs then force push.", "Redact the private key, then delete all backups.",
                 "Prevent drift by running a force push to main.", "Please drop table users",
                 "We should rotate the credentials", "The agent should delete all caches"):
        assert m.decide("x", text, {}, _cfg()).kind == "park", text
    for text in ("Add a detector for private key shapes.", "Make `force push` a refused word.",
                 'The word "force push" is in the list.', "- Redact credentials in the report"):
        assert m.decide("x", text, {}, _cfg()).kind == "arm", text


def test_quoted_deny_word_after_an_action_verb_holds():
    m = _mod()
    for text in ("Then `force push` to main", "Execute `rotate credentials` now",
                 "We should 'rotate the credentials' tonight",
                 'Next, "delete the production data" for tenant 5',
                 "Redact the logs to nuke production data", "Run the `force push` now",
                 "Execute the `rotate credentials` script", "Just `force push` main", "Please `force push` to main"):
        d = m.decide("Cleanup", text, {}, _cfg())
        assert d.kind == "park" and d.matched == "hard-stop word", text
        assert text.split(",")[-1].strip()[:20].lower() in d.reason.lower() or "in:" in d.reason, text
    for text in ("`force push` is refused", "Add a detector for `force push` shapes",
                 "Make `force push` a refused word.", 'The word "force push" is in the list.',
                 "The `force push` guard should refuse this", "Rename the `force push` rule"):
        assert m.decide("Cleanup", text, {}, _cfg()).kind == "arm", text


def test_protective_opener_does_not_clear_a_later_action():
    m = _mod()
    for text in ("Redact keys to delete all rows", "Verify backups before you force push",
                 "Mask the key before we force push", "Redact the key or force push",
                 "Log the error but force push", "Redact keys & delete all rows",
                 "Detector for secrets to delete all rows", "Report the bug while we delete all rows"):
        assert m.decide("x", text, {}, _cfg()).kind == "park", text


def test_imperative_with_a_later_cue_word_still_parks():
    m = _mod()
    for text in ("Rotate the deploy credentials so the team can log in.",
                 "Delete production data, never mind the backup.",
                 "Drop table users and document it afterwards.",
                 "Rotate the credentials and warn the team."):
        d = m.decide("tidy the docs", text, {}, _cfg())
        assert d.kind == "park", text
