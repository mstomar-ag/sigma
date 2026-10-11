# SPDX-License-Identifier: MIT
"""`model:*` is an adopter's own annotation, not a kit vocabulary — pinned as such (#1602).

Triage's hygiene bucket used to flag every in-scope goal that carried no `model:*` label, and the
`sigma-triage` SKILL presented that as a gap to close before picking. Nothing answered the only
question that raises: *which values are legal?* The kit creates nine labels, all `sdlc:*`
lifecycle (`test_the_kit_creates_only_the_nine_lifecycle_labels`); `docs/label-model.md` did not
mention the prefix; and the kit's own two hardcoded values contradicted each other and everything
else — `model:daily` on every decompose meta-issue, `model:bulk` in the README's copy-paste example,
neither defined anywhere, while `predict.py`'s tiers are `haiku | sonnet | opus | fable`.

THE DIRECTION TAKEN, AND WHY IT IS NOT THE OTHER ONE. The alternative was to define the vocabulary:
write the four tiers into `docs/label-model.md` and translate the kit's two stragglers into it. That
fails on a fact `test_the_tier_is_a_function_of_the_goal_text_alone` measures — **no label value
reaches model selection at all.** `predict.predict()` takes goal TEXT and nothing else; the gate is
`model_selection: "auto"` in config; `_label_value` reads `model:` only so triage can print a column.
Publishing a value set in the file whose whole job is "what each label MEANS" would therefore assert,
in the contract document, that setting `model:opus` runs a goal on opus. It does not. That is the
same defect as #1600 — a doc describing something the code does not do — committed fresh, in the one
place readers are entitled to trust. So the demand goes instead, and the doc says what is true.

A hygiene check is a claim that an issue is not enqueue-ready. `priority:*` earns that: triage reads
it and orders waves by it, so it stays demanded (`test_hygiene_still_demands_priority`). `model:*`
does not: the enqueue path, the pick path and the tier prediction all run identically without it.
Demanding it manufactured an obligation with no payoff, and the field result is what an unpayable
obligation always produces — one adopter board grew `model:sonnet`, `model:haiku` and `model:daily`,
this repo's grew five values, none agreeing with each other or with `predict.py`.

What deliberately SURVIVES is the pass-through: a human who puts a model value in a triage plan
still gets it attached (`test_a_human_authored_plan_still_attaches_its_model_value`). The kit takes
no position on the vocabulary — which is exactly why it must not demand one, and must not stop an
adopter who has one of their own.
"""
import importlib.util
import inspect
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


triage = _mod(SCRIPTS / "triage.py", "triage")
sources = _mod(SCRIPTS / "sources.py", "sources")
predict = _mod(ROOT / "skills" / "sigma-model" / "scripts" / "predict.py", "predict")

_GOAL, _PARKED = "sdlc:goal", "sdlc:parked"
_CFG = {"goal_label": _GOAL, "parked_label": _PARKED}


def _issue(number, labels=()):
    return {"number": number, "title": "x", "body": "",
            "labels": [{"name": n} for n in labels], "assignees": []}


# --------------------------------------------------------------- the demand is gone


def test_hygiene_no_longer_demands_a_model_label():
    """The defect itself: a goal with a priority and no `model:*` is enqueue-ready, and triage now
    agrees. This is the assertion that inverts `test_hygiene_flags_missing_model_or_priority`."""
    out = triage._bucket_hygiene([_issue(1, [_GOAL, "priority:P1"])], _CFG)
    assert out["items"] == [], out["items"]
    assert out["count"] == 0


def test_hygiene_still_demands_priority():
    """The over-correction guard. `priority:*` is READ -- triage orders waves by it -- so its
    absence is a real enqueue-readiness gap and must keep being reported."""
    out = triage._bucket_hygiene([_issue(1, [_GOAL])], _CFG)
    assert [i["number"] for i in out["items"]] == [1]
    assert out["items"][0]["missing"] == ["priority"]


def test_hygiene_still_reports_the_goal_and_parked_contradiction():
    """The bucket's other job (#817) is untouched by dropping the model check."""
    out = triage._bucket_hygiene([_issue(2, [_GOAL, _PARKED, "priority:P1"])], _CFG)
    assert [i.get("contradiction") for i in out["items"]] == ["goal+parked"]


def test_a_model_label_is_neither_required_nor_rejected():
    """Adopters who DO keep a tier vocabulary are not broken by this: carrying one is still fine,
    it simply is not demanded. The kit takes no position either way."""
    for labels in ([_GOAL, "priority:P1"], [_GOAL, "priority:P1", "model:opus"],
                   [_GOAL, "priority:P1", "model:whatever-they-like"]):
        assert triage._bucket_hygiene([_issue(1, labels)], _CFG)["count"] == 0, labels


# --------------------------------------------------------------- why demanding it was wrong


#: Parameters that are NOT a label value, each with why it is not one. The point of this list is
#: that it has to be EDITED: a new parameter fails the test below until somebody states what it
#: carries, which is the tripwire the original assertion was reaching for. Pinning the signature
#: literally caught the wrong thing -- #1601 added a config-derived exclusion list and the test fired
#: on it, which is a false positive on the exact claim it exists to defend.
_NOT_A_LABEL = (
    "excludes",        # #1601: signal literals from `model_selection_signal_excludes`, not a label
    # #2564: a PRICE ceiling from `model_selection_max_tier`. Not a label, and specifically not a
    # `model:*` one: it is read from .sdlc/config.json by `resolve`/`resolve_step`/`main`, it names
    # a tier the router may not EXCEED rather than one a goal should RUN at, and it cannot raise a
    # goal's tier under any input (pinned by test_the_cap_can_never_raise_the_resulting_price). So
    # the direction taken in #1602 stands unchanged and docs/label-model.md §12 needs no edit.
    "max_tier",
)


def test_the_tier_is_a_function_of_the_goal_text_alone():
    """The load-bearing fact behind the direction taken. `predict()` has ONE parameter and it is
    the goal text -- there is no label argument, so no `model:*` value can influence the tier. Read
    off the live signature rather than asserted in prose, so the day a label DOES become an input,
    this fails and the documentation is forced to change with it."""
    for fn in (predict.predict, predict.predict_with_reason):
        params = list(inspect.signature(fn).parameters)
        assert params[0] == "goal_text", fn.__name__
        unexplained = [p for p in params[1:] if p not in _NOT_A_LABEL]
        assert unexplained == [], (
            "%s gained %r. If that carries a `model:*` value the direction taken in #1602 is wrong "
            "and docs/label-model.md \u00a712 must change with it; if it does not, name it in "
            "_NOT_A_LABEL with the reason." % (fn.__name__, unexplained))
    assert "sdlc_dir" in inspect.signature(predict.resolve).parameters   # config gate, not a label


def test_the_model_selection_path_never_reads_a_label():
    """The same claim one level up: the module that picks the tier contains no reference to the
    label prefix at all. Cheap, blunt, and exactly the assertion that would have caught someone
    wiring the label in without updating the contract doc."""
    assert "model:" not in (ROOT / "skills" / "sigma-model" / "scripts" / "predict.py").read_text(
        encoding="utf-8")


def test_the_kit_creates_only_the_nine_lifecycle_labels():
    """`docs/label-model.md` now states that the kit seeds ten labels (seven, plus
    `sdlc:designed` as of #1826, plus `sdlc:needs-unit` as of #2263, plus `sdlc:needs-triage` as of
    #2363) and all of them are `sdlc:*` lifecycle. Pinned against the real table `_ensure_labels`
    iterates, so the claim cannot rot into a lie the way the ledger's filename did."""
    attrs = [attr for attr, _colour in sources.GitHubSource._LABEL_COLORS]
    assert len(attrs) == 9, attrs
    src = (SCRIPTS / "sources.py").read_text(encoding="utf-8")
    for attr in attrs:
        default = re.search(rf'self\.{attr} = .*?"(sdlc:[a-z-]+)"', src)
        assert default, f"{attr} has no sdlc:* default -- the doc's claim no longer holds"


# --------------------------------------------------------------- no undefined value ships


#: `tests/` is exempt (it names values on purpose, as fixtures) and so are the dated design records
#: under `docs/superpowers/`, which describe what was true when they were written.
_CODE = [p for p in (ROOT / "skills").rglob("*")
         if p.is_file() and p.suffix in (".py", ".json", ".tmpl", ".sh")]
_PROSE = [ROOT / "README.md", ROOT / "docs" / "label-model.md"] + [
    p for p in (ROOT / "skills").rglob("*.md") if p.is_file()]

#: A concrete VALUE inside a STRING LITERAL -- the shape that gets attached to an issue, and
#: therefore `gh label create --force`d onto the adopter's board on the way past. Bare `model:` (the
#: prefix constant), `model:*` and `model:` in a sentence are all deliberately fine: this test bans
#: the kit SHIPPING a value, not the kit TALKING about one. §12 of `docs/label-model.md` has to be
#: able to write "putting `model:opus` on an issue does not run it on Opus", and a rule that forbade
#: naming the thing being corrected would forbid the correction.
_LITERAL = re.compile(r"""["']model:[A-Za-z0-9]""")

#: The same value inside a fenced block, which is the markdown equivalent of a literal: a reader
#: pastes it and their board grows the label. Prose outside a fence is explanation, not instruction.
_FENCE = re.compile(r"```.*?```", re.S)


def test_no_shipped_code_hardcodes_a_model_label_value():
    """`loop.py` attached `"model:daily"` to every decompose meta-issue -- and
    `create_tracked_issue` runs `gh label create --force` for each extra label, so the kit was not
    merely using an undefined value, it was CREATING it on every adopter board it touched. That is
    how "daily" reached boards whose owners had never typed it."""
    offenders = [f"{p.relative_to(ROOT)}: {m.group(0)}"
                 for p in _CODE
                 for m in _LITERAL.finditer(p.read_text(encoding="utf-8", errors="ignore"))]
    assert not offenders, ("shipped code hardcodes a model:* value the kit defines nowhere:\n"
                           + "\n".join(sorted(set(offenders))))


def test_no_copy_pasteable_example_hands_an_adopter_a_model_label_value():
    """The README demonstrated `--label sdlc:followup,model:bulk` in a runnable block and called it
    "the model tier". An adopter who pastes that gets a `model:bulk` label, believes it selected a
    tier, and has in fact changed nothing about how the goal runs."""
    offenders = []
    for p in _PROSE:
        for fence in _FENCE.findall(p.read_text(encoding="utf-8", errors="ignore")):
            for m in re.finditer(r"model:[A-Za-z0-9]+", fence):
                offenders.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert not offenders, ("a runnable example hands out a model:* value:\n"
                           + "\n".join(sorted(set(offenders))))


def test_the_decompose_meta_issue_files_with_its_lifecycle_label_only():
    """The one behavioural consequence of the line above: a decompose meta-issue still carries
    `sdlc:decompose` (which `decompose_check` reads) and no longer drags an undefined tier label --
    and therefore no longer CREATES one on every adopter board it touches."""
    src = (SCRIPTS / "loop.py").read_text(encoding="utf-8")
    assert 'extra_labels=["sdlc:decompose"]' in src
    assert "model:daily" not in src


# --------------------------------------------------------------- the doc says what is true


def _label_model():
    return (ROOT / "docs" / "label-model.md").read_text(encoding="utf-8")


def test_the_label_contract_covers_the_model_prefix():
    """The gap that made this a defect rather than a preference: triage named a prefix the contract
    document had never heard of."""
    doc = _label_model()
    assert "model:*" in doc


def test_the_contract_names_the_mechanism_that_actually_picks_the_tier():
    """A reader who has just been told the label does nothing must be sent to the thing that does,
    or the doc has replaced a wrong answer with no answer."""
    doc = _label_model()
    assert "model_selection" in doc
    assert "/sigma-model" in doc or "predict.py" in doc


def test_the_contract_states_that_no_value_set_is_defined_or_read():
    """Both halves matter and they are different claims: the kit defines no values (so no value is
    'wrong'), and reads none (so no value has an effect)."""
    doc = _label_model().lower()
    assert "does not define" in doc or "defines no" in doc
    assert "reads no" in doc or "does not read" in doc or "nothing reads" in doc


def test_the_triage_skill_no_longer_presents_a_missing_model_label_as_a_gap():
    """The SKILL is what an agent running triage reads; leaving it demanding the label would keep
    the pressure on regardless of what the bucket now reports."""
    skill = (ROOT / "skills" / "sigma-triage" / "SKILL.md").read_text(encoding="utf-8")
    assert "missing `priority:*` / `model:*`" not in skill
    assert "missing `priority:*` and `model:*` labels" not in skill


# --------------------------------------------------------------- what must NOT change


def test_a_human_authored_plan_still_attaches_its_model_value():
    """The pass-through survives. An adopter with their own tier vocabulary puts it in the triage
    plan and `enact` attaches it -- the kit still carries a value it does not interpret, which is
    the whole posture. Dropping this with the demand would have been the over-correction."""
    plan = {"picked": [{"issue": 7, "priority": "P1", "model": "opus"}]}
    state = {"7": {"labels": set(), "assignees": set(), "body": ""}}
    actions = triage.compute_actions(plan, state, assignee=None, goal_label=_GOAL,
                                     parked_label=_PARKED)
    added = [a["detail"] for a in actions if a["action"] == "add-label"]
    assert "model:opus" in added, actions
    assert triage._label_create_calls(plan) == ["priority:P1", "model:opus"]
