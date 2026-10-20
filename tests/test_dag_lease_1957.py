"""#1957: one refresher per repository for the dependency map, by a lease on the ledger branch.

Offline: fixture issue numbers, real local git repos with one bare origin (two "machines" are two clones with
different host aliases), a fake `gh` runner that counts calls. No network, no model.
MEASURED: these tests only, including the two-machine scenario in `test_two_machines_two_hours_reads`;
the live number of reads across real machines is NOT measured.

Controls, each run red once on its own clause (see the final report of the goal):
  * drop the expiry check in `_read_all`                      -> the expired-lease hand-over test red
  * drop the foreign-holder check in `acquire`                -> the foreign-holder fold test red (its no-lease assertion) and the scenario test
  * tiebreak weakened (last file in place of the smallest key) -> the tiebreak test and the race test red
    (a plain-existence weakening, 'any other lease means fold', leaves both machines folded: the race test's
    one-holder assertion goes red the same way)
  * drop lease staging in `sync.publish`                      -> the lease-only publish test red (and three more)
  * drop the snapshot-age fold in `acquire`                   -> the fresh-snapshot fold test red
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401  (any subprocess env built here would carry SIGMA_RESOURCE_MODE)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sync, dag_store, dag_lease, dag_refresh, dag_cli = (_mod(n) for n in ("sync", "dag_store", "dag_lease", "dag_refresh", "dag"))

NOW = 1_800_000_000.0
TTL = 3600


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout.strip()


def _config(alias, enabled=True):
    return {"ledger": {"enabled": enabled, "actor": "dana", "host_alias": alias},
            "discovery": {"source": "github", "github": {"repo": "owner/repo"}, "dag": {"mode": "on"}}}


class Gh:
    """A scripted repository that counts calls (five list reads make one collection)."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[1].endswith("/issues"):
            if "labels=sdlc:goal" in args:
                return json.dumps([{"number": 8, "title": "t", "body": "", "state": "open",
                                    "labels": [{"name": "sdlc:goal"}], "updated_at": "2026-10-01T00:00:00Z"}])
            return "[]"
        raise AssertionError("unexpected call %r" % (args,))


class Machine:
    def __init__(self, base, cfg, name):
        self.base, self.cfg, self.name, self.gh = base, cfg, name, Gh()

    @property
    def root(self):
        return self.base / "ledger"

    def lease_file(self):
        return self.root / "dag" / "lease" / ("dana-%s.json" % self.cfg["ledger"]["host_alias"])

    def tick(self, now):
        """What the watcher does: the refresh duty, then the publish."""
        res = dag_refresh.refresh_if_due(self.base, self.cfg, self.gh, now)
        sync.publish(self.base, self.cfg)
        return res

    def reads(self):
        return len(self.gh.calls)


@pytest.fixture
def origin(tmp_path):
    o = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(o)], check=True)
    return o


def _machine(tmp_path, origin, name, enabled=True):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"
    (base / "state").mkdir(parents=True)
    cfg = _config(name, enabled)
    (base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if enabled:
        out = sync.bootstrap(base, cfg)
        assert "published" in out or "bootstrapped" in out or "joined" in out or sync.is_worktree(base), out
    return Machine(base, cfg, name)


@pytest.fixture
def two(tmp_path, origin):
    return _machine(tmp_path, origin, "boxa"), _machine(tmp_path, origin, "boxb")


def _plain_lease(m, writer, taken, span=TTL + 360):
    d = m.root / "dag" / "lease"
    d.mkdir(parents=True, exist_ok=True)
    (d / (writer + ".json")).write_text(json.dumps({"writer": writer, "taken_at": taken, "expires_at": taken + span}))


# ------------------------------------------------------------------ who reads

def test_only_the_holder_reads_and_the_other_folds_the_snapshot_in(two):
    a, b = two
    assert a.tick(NOW)["status"] == "saved" and a.reads() == 5
    assert a.lease_file().is_file()
    res = b.tick(NOW + 10)
    assert res["status"] == "folded" and b.reads() == 0
    assert not b.lease_file().exists()
    m, why, src = dag_store.load_with_source(b.base, b.cfg)          # B now has A's map from the ledger
    assert (why, src) == ("ok", "ledger") and m["nodes"][0]["number"] == 8


def test_foreign_unexpired_holder_folds_before_any_snapshot_exists(two):
    """A took the lease but has not published a snapshot yet: B still makes no call.
    control: drop the foreign-holder check in `acquire` -> B writes a lease it then loses (the tiebreak alone
    still keeps it from reading, so the no-lease assertion is what goes red here, and the scenario test)."""
    a, b = two
    assert dag_lease.acquire(a.base, a.cfg, NOW) == "holder"
    assert dag_store.read_snapshot(b.base, b.cfg) is None
    res = b.tick(NOW + 5)
    assert res["status"] == "folded" and b.reads() == 0
    assert not b.lease_file().exists()                # the check stops it before a lease is written, not after
    assert dag_refresh.read_meta(b.base)["last_status"] == "folded"
    assert dag_refresh.is_due(b.base, b.cfg, NOW + 60) == (False, "retry-wait")   # a fold is not retried every tick
    assert dag_refresh.is_due(b.base, b.cfg, NOW + 400)[1] != "retry-wait"


def test_fresh_snapshot_folds_with_zero_calls_even_with_no_lease(tmp_path, origin):
    """A snapshot younger than the TTL means nobody reads, whoever holds what.
    control: drop the snapshot-age fold in `acquire` -> B takes the lease and reads."""
    a, b = _machine(tmp_path, origin, "boxa"), _machine(tmp_path, origin, "boxb")
    _stored = {"header": {"repo": "owner/repo", "built_at": "2027-01-15T08:00:00Z", "last_ok_at": "2027-01-15T08:00:00Z"},
               "nodes": [{"number": 8, "kind": "issue", "state": "open", "life": "goal"}], "edges": []}
    built = dag_store.parse_ts("2027-01-15T08:00:00Z")
    assert dag_store.save(a.base, _stored, a.cfg)[0]
    sync.publish(a.base, a.cfg)                                       # a snapshot on the branch, no lease file
    assert not a.lease_file().exists()
    res = dag_refresh.refresh_if_due(b.base, b.cfg, b.gh, built + 1200, force=False)
    assert b.reads() == 0 and res["status"] in ("fresh", "folded")
    assert dag_lease.acquire(b.base, b.cfg, built + 1200) == "fold" and not b.lease_file().exists()
    assert dag_lease.acquire(b.base, b.cfg, built + TTL + 100) == "holder"      # older than the TTL: take it


def test_expired_lease_is_handed_over_without_a_person(two):
    """A read at NOW and died; once its lease lapses B is due and takes over.
    control: drop the expiry check in `_read_all` -> B folds forever."""
    a, b = two
    a.tick(NOW)
    assert b.tick(NOW + 60)["status"] == "folded"
    later = NOW + TTL + 600                                          # past the lease (TTL plus margin) and the TTL
    assert b.tick(later)["status"] == "saved" and b.reads() == 5
    assert b.lease_file().is_file()
    assert dag_lease.holder(b.root, later)["writer"] == "dana-boxb"


# ------------------------------------------------------------------ the tiebreak

def test_holder_is_the_earliest_taken_then_the_smaller_writer(tmp_path, origin):
    m = _machine(tmp_path, origin, "boxa")
    _plain_lease(m, "dana-z", NOW + 5)
    _plain_lease(m, "dana-y", NOW + 5)
    _plain_lease(m, "dana-q", NOW + 9)
    assert dag_lease.holder(m.root, NOW + 10)["writer"] == "dana-y"
    _plain_lease(m, "dana-late", NOW + 1)
    assert dag_lease.holder(m.root, NOW + 10)["writer"] == "dana-late"


def test_race_where_both_write_before_pulling_ends_with_one_holder(two, monkeypatch):
    """Both machines write a lease before either sees the other's; after the exchange both read the same
    two files and agree on one holder.
    control: tiebreak weakened to plain existence (any other lease means fold) -> both fold, none holds."""
    a, b = two
    real = dag_lease._exchange
    monkeypatch.setattr(dag_lease, "_exchange", lambda *args, **kw: None)         # nobody exchanges yet
    dag_lease.acquire(a.base, a.cfg, NOW, force=True)
    dag_lease.acquire(b.base, b.cfg, NOW + 3, force=True)
    monkeypatch.setattr(dag_lease, "_exchange", real)
    sync.publish(a.base, a.cfg)
    sync.publish(b.base, b.cfg)                                     # B's publish rebases onto A's lease
    sync.pull(a.base, a.cfg)
    verdicts = [dag_lease.verdict(m.root, "dana-" + m.name, NOW + 10, TTL) for m in (a, b)]
    assert sorted(verdicts) == ["fold", "holder"] and verdicts[0] == "holder"      # A took it first


def test_lease_only_change_publishes(two, origin):
    """control: drop lease staging in `sync.publish` -> 'nothing to publish' and the branch has no lease."""
    a, _b = two
    dag_lease._write(a.root, "dana-boxa", NOW, TTL)
    assert not (a.root / "dag" / "map.json").exists()
    out = sync.publish(a.base, a.cfg)
    assert "nothing" not in out, out
    shown = json.loads(_git(origin, "show", "sdlc-" "ledger:dag/lease/dana-boxa.json"))
    assert shown["writer"] == "dana-boxa" and shown["expires_at"] == NOW + TTL + 360


def test_forged_or_odd_lease_files_are_ignored(tmp_path, origin):
    m = _machine(tmp_path, origin, "boxa")
    d = m.root / "dag" / "lease"
    d.mkdir(parents=True)
    (d / "dana-fut.json").write_text(json.dumps({"writer": "dana-fut", "taken_at": NOW + 99999, "expires_at": NOW + 99999 + TTL}))
    (d / "dana-wide.json").write_text(json.dumps({"writer": "dana-wide", "taken_at": NOW, "expires_at": NOW + 10 * TTL}))
    (d / "dana-name.json").write_text(json.dumps({"writer": "other", "taken_at": NOW, "expires_at": NOW + TTL}))
    (d / "dana-bad.json").write_text("{not json")
    (d / "dana-bool.json").write_text(json.dumps({"writer": "dana-bool", "taken_at": True, "expires_at": NOW + TTL}))
    assert dag_lease.holder(m.root, NOW + 1, TTL) is None
    assert dag_lease.acquire(m.base, m.cfg, NOW + 1) == "holder"


# ------------------------------------------------------------------ ledger off, force, rebuild

def test_ledger_off_reads_as_today_and_creates_no_dag_dir(tmp_path, origin):
    m = _machine(tmp_path, origin, "boxa", enabled=False)
    assert dag_lease.acquire(m.base, m.cfg, NOW) == "local"
    assert dag_refresh.refresh_if_due(m.base, m.cfg, m.gh, NOW)["status"] == "saved"
    assert m.reads() == 5
    assert not (m.base / "ledger").exists()


def test_force_reads_while_another_machine_holds_and_takes_the_lease(two):
    a, b = two
    a.tick(NOW)
    assert dag_refresh.refresh_if_due(b.base, b.cfg, b.gh, NOW + 20, force=True)["status"] == "saved"
    assert b.reads() == 5 and b.lease_file().is_file()
    assert dag_lease.holder(b.root, NOW + 30)["writer"] == "dana-boxa"         # A's older lease still wins the record


def test_rebuild_takes_the_lease_and_reads(two):
    """`dag.py rebuild` reads on this machine though another holds the lease, and takes its own lease. The
    suite's guard refuses any real `gh` subprocess, so the read is attempted (list 1) and fails inside the
    process: no network."""
    a, b = two
    a.tick(NOW)
    out, _code = dag_cli.rebuild(b.base)
    assert out.startswith("dag rebuild:") and "calls list 1" in out, out
    assert b.lease_file().is_file()


# ------------------------------------------------------------------ the scenario in docs/dag.md

def test_two_machines_two_hours_reads(tmp_path):
    """Two machines, a 60 minute TTL, a tick every 5 minutes for two hours (inclusive). Before: ledger off, each
    machine reads for itself. After: ledger on, one reads. Counted as collections (five list calls each)."""
    def run(enabled):
        o = tmp_path / ("origin-%s.git" % enabled)
        subprocess.run(["git", "init", "-q", "--bare", str(o)], check=True)
        w = tmp_path / ("w%s" % enabled)
        w.mkdir()
        ms = [_machine(w, o, n, enabled) for n in ("boxa", "boxb")]
        for t in range(0, 121, 5):
            for m in ms:
                m.tick(NOW + t * 60) if enabled else dag_refresh.refresh_if_due(m.base, m.cfg, m.gh, NOW + t * 60)
        return sum(m.reads() for m in ms) // 5
    before, after = run(False), run(True)
    assert (before, after) == (4, 2), (before, after)
