"""#1617: `gh_api.conditional_get`, the header-aware GET with a stored ETag.

The fake runner emits EXACTLY the shapes captured from the real CLI in tests/fixtures/gh_api_include/
(see its README.txt): 200 with a weak ETag exits 0; 304 exits 1 with the status line and headers on
stdout and `gh: HTTP 304` on stderr; 404 exits 1 with a JSON body. No network and no `gh` here.

control (304 mapped to a failure): in `conditional_get` let the 304 branch raise -> the 304 test
goes red (run once, then restored)."""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
FIX = ROOT / "tests" / "fixtures" / "gh_api_include"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gh_api = _mod("gh_api")


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class Runner:
    def __init__(self, rc, out, err=""):
        self.rc, self.out, self.err, self.calls = rc, out, err, []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.rc, self.out, self.err


def test_200_with_weak_etag_parses_body_and_keeps_the_prefix():
    run = Runner(0, fixture("200_etag.txt"))
    got = gh_api.conditional_get("repos/o/r/issues", run=run, fields={"state": "all"})
    assert got["status"] == 200 and not got["not_modified"]
    assert got["etag"].startswith('W/"') and got["etag"].endswith('"')
    assert got["body"][0]["number"] == 7
    assert run.calls[0][:2] == ["api", "repos/o/r/issues"] and "-i" in run.calls[0]
    assert "state=all" in run.calls[0]
    assert not any(a.startswith("If-None-Match") for a in run.calls[0])


def test_etag_is_sent_back_exactly_as_stored():
    run = Runner(1, fixture("304.txt"), "gh: HTTP 304\n")
    etag = 'W/"cdabd6c2369e193d105096d0a09adb1f8d7303ddfe82fd8b9d0eca1512005a7d"'
    gh_api.conditional_get("repos/o/r/issues", etag=etag, run=run)
    args = run.calls[0]
    assert args[args.index("-H") + 1] == "If-None-Match: " + etag


def test_304_exit_1_is_not_modified_not_an_error():
    """control: treat 304 as a failure (raise) -> red."""
    run = Runner(1, fixture("304.txt"), "gh: HTTP 304\n")
    got = gh_api.conditional_get("repos/o/r/issues", etag='W/"x"', run=run)
    assert got["status"] == 304 and got["not_modified"] and got["body"] is None
    assert len(run.calls) == 1


def test_404_raises_a_classified_error():
    run = Runner(1, fixture("404.txt"), "gh: Not Found (HTTP 404)\n")
    with pytest.raises(gh_api.GhApiError) as ei:
        gh_api.conditional_get("repos/o/r/issues", run=run)
    assert ei.value.status == 404 and ei.value.kind == "not_found"


def test_exit_1_without_a_status_line_is_an_error_not_a_304():
    run = Runner(1, "", "gh: dial tcp: i/o timeout\n")
    with pytest.raises(gh_api.GhApiError) as ei:
        gh_api.conditional_get("repos/o/r/issues", run=run)
    assert ei.value.kind == "transport"


def test_timeout_is_a_transport_error():
    run = Runner(124, "", "gh: the call timed out after 120s")
    with pytest.raises(gh_api.GhApiError):
        gh_api.conditional_get("repos/o/r/issues", run=run)


def test_unparseable_200_body_raises():
    run = Runner(0, "HTTP/2.0 200 OK\nEtag: W/\"a\"\n\nnot json")
    with pytest.raises(gh_api.GhApiError):
        gh_api.conditional_get("repos/o/r/issues", run=run)


def test_a_runner_exception_is_wrapped():
    def boom(args):
        raise OSError("no gh")
    with pytest.raises(gh_api.GhApiError):
        gh_api.conditional_get("repos/o/r/issues", run=boom)
