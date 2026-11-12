"""
Tests for the write rules of a normal run (main()).

A posting written to state.json is never reported again, so writing one that
was not scored loses it for good. Two rules follow:

  - if scoring is enabled but cannot run, nothing is written at all
  - postings over max_new_per_run are left unseen for the next run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rolegenie                                             # noqa: E402
from rolegenie import Posting                                # noqa: E402


# CR-rg-hydrate-gate: scoring requires the full advert (>= HYDRATED_MIN_CHARS), so
# a test posting that is meant to be scored carries a realistic full-length body.
FULL = ("We are hiring a Platform Specialist to build and ship production "
        "systems with enterprise customers. ") * 12


def postings(n):
    return [Posting(uid=f"ExampleCo:{i}", board="ExampleCo", title=f"Role {i}",
                    location="London", url=f"https://x/{i}", body=FULL) for i in range(n)]


@pytest.fixture
def run(tmp_path, monkeypatch):
    """main() against a throwaway config, state and digest dir, no network."""
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    cfg = {"boards": [],
           "scoring": {"enabled": True, "max_new_per_run": 2, "scorer": "fake",
                       "scorers": {"fake": ["fake-cli", "-p"]}},
           "output": {"state_file": str(state), "digest_dir": str(tmp_path / "digests"),
                      "email": {"enabled": False}}}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    monkeypatch.setattr(rolegenie, "CONFIG_PATH", config)
    monkeypatch.setenv("ROLEGENIE_HOME", str(tmp_path / "home"))  # jd/, cards/ stay in tmp (CR-rg-user-data-home)
    monkeypatch.setattr(rolegenie, "matches", lambda p, c: True)
    monkeypatch.setattr(rolegenie, "hydrate", lambda ps: None)
    monkeypatch.setattr(rolegenie, "build_profile", lambda cfg: "test profile")
    monkeypatch.setattr(sys, "argv", ["rolegenie.py"])

    def go(live):
        monkeypatch.setattr(rolegenie, "fetch_all", lambda boards: (live, []))
        return rolegenie.main()

    go.state = state
    go.digests = tmp_path / "digests"
    return go


def test_missing_cli_writes_nothing(run, monkeypatch):
    monkeypatch.setattr(rolegenie.shutil, "which", lambda name: None)
    before = run.state.read_bytes()

    assert run(postings(1)) == 1
    assert run.state.read_bytes() == before
    assert not run.digests.exists()


def test_every_call_failing_writes_nothing(run, monkeypatch):
    # A signed-out CLI answers every call with an error. That is no scorer,
    # not a run of bad postings, so nothing may be marked seen.
    monkeypatch.setattr(rolegenie.shutil, "which", lambda name: "claude")

    def signed_out(*a, **kw):
        return rolegenie.subprocess.CompletedProcess(
            a, 1, stdout='{"is_error": true, "result": "Not logged in"}', stderr="")
    monkeypatch.setattr(rolegenie.subprocess, "run", signed_out)
    before = run.state.read_bytes()

    assert run(postings(2)) == 1
    assert run.state.read_bytes() == before


def test_overflow_is_left_for_the_next_run(run, monkeypatch):
    def fake_score(ps, cfg, examples="", profile=""):
        for p in ps:
            p.score, p.verdict = 7, "ok"
        return True
    monkeypatch.setattr(rolegenie, "score_postings", fake_score)

    assert run(postings(5)) == 0
    assert set(json.loads(run.state.read_text())) == {"ExampleCo:0", "ExampleCo:1"}

    # The next run scores the next two, still nothing unscored in state.
    assert run(postings(5)) == 0
    seen = json.loads(run.state.read_text())
    assert set(seen) == {"ExampleCo:0", "ExampleCo:1", "ExampleCo:2", "ExampleCo:3"}
    assert all(v["score"] == 7 for v in seen.values())


def test_prompt_goes_on_stdin_or_in_place_of_the_placeholder(monkeypatch, fit_stub, reqs):
    # codex/claude/ollama read stdin; agy only takes the prompt as an argument.
    monkeypatch.setattr(rolegenie.shutil, "which", lambda name: name)
    calls = []

    def fake_run(argv, input=None, **kw):
        calls.append((argv, input))
        return rolegenie.subprocess.CompletedProcess(
            argv, 0, stdout='```json\n{"score": 6, "verdict": "fine"}\n```', stderr="")
    monkeypatch.setattr(rolegenie.subprocess, "run", fake_run)

    for cmd in (["stdin-cli", "-"], ["arg-cli", "-p", "{prompt}"]):
        p = postings(1)[0]
        p.requirements = reqs()  # fit score from Jev (stubbed); this test is about CLI plumbing
        cfg = {"scoring": {"scorer": "x", "scorers": {"x": cmd}}}
        assert rolegenie.score_postings([p], cfg) is True
        assert (p.score, p.verdict) == (10, "fine")

    (stdin_argv, stdin_in), (arg_argv, arg_in) = calls
    assert stdin_argv == ["stdin-cli", "-"] and "Role 0" in stdin_in
    assert arg_in is None and "Role 0" in arg_argv[-1] and "{prompt}" not in arg_argv
