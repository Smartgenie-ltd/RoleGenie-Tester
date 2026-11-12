"""RG-S05 acceptance: applying (CR-rg-s05). No browser, model or email is used."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import apply
import board
import brief

NOW = "2026-09-27T09:00:00"
SHA = "master-v1"


def entry(**over):
    e = {"board": "Acme", "title": "Architect", "url": "https://acme.example/jobs/1", "score": 8,
         "cv": {"status": "drafted", "pdf": "C:/Jobs/Acme/cv.pdf", "at": "2026-09-27T07:10:00", "master_sha": SHA},
         "decisions": [{"decision": "apply", "at": "2026-09-27T08:00:00"}],
         "package": {"status": "ready", "auto_submit": True, "master_sha": SHA, "channel": "lever",
                     "approved": {"at": "2026-09-27T08:00:00"}}}
    e.update(over)
    return e


@pytest.fixture
def env(tmp_path, monkeypatch):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    answers = tmp_path / "answers.md"
    answers.write_text("1. Right to work: yes", encoding="utf-8")
    monkeypatch.setattr(apply, "ANSWERS", answers)
    monkeypatch.setattr(apply, "ROOT", tmp_path)
    import tailor
    monkeypatch.setattr(tailor, "current_master_sha", lambda cfg: SHA)
    sf = tmp_path / "state.json"

    def write(state):
        for e in state.values():
            if e.get("cv", {}).get("pdf"):
                e["cv"]["pdf"] = str(pdf)
        sf.write_text(json.dumps(state), encoding="utf-8")
    cfg = {"output": {"state_file": "state.json"}, "apply": {"enabled": True}}
    return write, sf, cfg


def test_queue_needs_apply_checked_cv_and_pdf():
    assert apply.is_queued(entry(), SHA)
    assert not apply.is_queued(entry(decisions=[{"decision": "shortlisted", "at": "x"}]), SHA)
    assert not apply.is_queued(entry(cv={"status": "failed", "pdf": "x", "master_sha": SHA}), SHA)
    assert not apply.is_queued(entry(applications=[{"status": "submitted", "at": "x"}]), SHA)
    assert not apply.is_queued(entry(), "master-v2")          # CV from an older master is never sent


def test_a_held_attempt_waits_for_a_new_tap():
    e = entry(applications=[{"status": "held", "at": "2026-09-27T08:05:00"}])
    assert apply.is_queued(e, SHA) and not apply.needs_attempt(e, SHA)
    e["decisions"].append({"decision": "apply", "at": "2026-09-27T08:30:00"})
    assert not apply.needs_attempt(e, SHA)  # another Apply decision alone cannot retry
    e["package"]["approved"] = {"at": "2026-09-27T08:30:00"}
    assert apply.needs_attempt(e, SHA)


def test_a_cv_drafted_after_the_tap_is_never_sent_unseen():
    assert not apply.needs_attempt(entry(cv={"status": "drafted", "pdf": "x", "at": "2026-09-27T08:30:00",
                                             "master_sha": SHA}), SHA)


@pytest.mark.parametrize("over,reason", [
    ({"decisions": [{"decision": "consider", "at": "x"}]}, "INV-RG-03"),
    ({"excluded": "requires German"}, "exclusion"),
])
def test_blockers(over, reason):
    state = {"A:1": entry(**over)}
    assert any(reason in r for r in apply.submit_blockers("A:1", state, NOW))


def test_company_limit_two_in_thirty_days_and_published_limit_wins():
    sub = lambda at: {"status": "submitted", "at": at}  # noqa: E731
    state = {"A:1": entry(), "A:2": entry(applications=[sub("2026-09-20T00:00:00")]),
             "A:3": entry(applications=[sub("2026-09-10T00:00:00")]),
             "A:4": entry(applications=[sub("2026-08-01T00:00:00")])}      # outside 30 days
    assert any("company limit" in r for r in apply.submit_blockers("A:1", state, NOW))
    assert apply.submit_blockers("A:1", state, NOW, {"Acme": (3, 30)}) == []


@pytest.mark.parametrize("r,ok", [
    ({"status": "submitted", "need": "", "confirmation": "Thanks"}, True),
    ({"status": "held", "need": "CAPTCHA", "confirmation": ""}, True),
    ({"status": "held", "need": " ", "confirmation": ""}, False),
    ({"status": "submitted", "need": "", "confirmation": ""}, False),
    ({"status": "done"}, False), ("text", False),
])
def test_result_check_mirrors_contract(r, ok):
    assert (apply.result_problems(r) == []) is ok


def test_prompt_has_job_cv_and_answers(env):
    write, sf, cfg = env
    write({"A:1": entry()})
    prompt = apply.build_prompt(json.loads(sf.read_text())["A:1"])
    assert "https://acme.example/jobs/1" in prompt and "cv.pdf" in prompt and "Right to work: yes" in prompt
    assert "{answers}" not in prompt and "sign in" in prompt.lower()


def test_blocked_posting_is_held_without_running_the_applier(env, monkeypatch):
    write, sf, cfg = env
    write({"A:1": entry(excluded="quota-carrying sales role")})
    monkeypatch.setattr(apply, "run_applier", lambda p: pytest.fail("must not run"))
    attempt = apply.apply_one("A:1", cfg)
    assert attempt["status"] == "held" and "exclusion" in attempt["need"]
    assert json.loads(sf.read_text())["A:1"]["applications"][-1]["status"] == "held"


def test_applier_result_is_recorded_append_only(env, monkeypatch):
    write, sf, cfg = env
    write({"A:1": entry()})
    monkeypatch.setattr(apply, "run_applier", lambda p: {"status": "submitted", "need": "", "confirmation": "Thanks"})
    assert apply.apply_one("A:1", cfg)["status"] == "submitted"
    assert apply.apply_one("A:1", cfg) is None                     # already submitted: nothing to do
    apps = json.loads(sf.read_text())["A:1"]["applications"]
    assert [a["status"] for a in apps] == ["submitted"]


def test_second_runner_is_turned_away_while_one_is_applying(env):
    write, sf, cfg = env
    write({"A:1": entry()})
    apply._lock_path("A:1").write_text("")
    with pytest.raises(apply.Busy):
        apply.apply_one("A:1", cfg)


def test_unreadable_applier_output_is_a_failure_never_a_submission(monkeypatch):
    class P:
        stdout = "I think it went fine"
    monkeypatch.setattr(apply.shutil, "which", lambda n: "claude")
    monkeypatch.setattr(apply.subprocess, "run", lambda *a, **k: P())
    r = apply.run_applier("prompt")
    assert r["status"] == "failed" and "may or may not" in r["need"]


def test_apply_tap_drafts_a_cv_when_missing_or_stale():
    # CR-rg-cv-on-apply: an Apply tap drafts the CV when there is none for the current
    # master (first draft) or it was drafted from an older master (re-draft).
    started = []
    start = lambda uid, cmd: started.append((uid, cmd))  # noqa: E731
    assert apply.on_apply_tap(entry(), "A:1", "master-v2", True, start) == "draft"  # stale master
    assert apply.on_apply_tap(entry(cv={"status": "drafted", "pdf": "x", "at": "1"}), "A:2", SHA, False, start)         == "draft"                                                    # no fingerprint yet: re-draft
    assert apply.on_apply_tap(entry(cv={}), "A:3", SHA, False, start) == "draft"    # no CV at all: first draft
    assert started == [("A:1", "tailor"), ("A:2", "tailor"), ("A:3", "tailor")]


def test_apply_tap_prepares_with_either_switch_setting():
    started = []
    start = lambda uid, cmd: started.append((uid, cmd))  # noqa: E731
    e = entry(cv={"status": "drafted", "pdf": "x", "at": "2026-01-01T00:00:00", "master_sha": SHA})
    assert apply.on_apply_tap(e, "A:1", SHA, False, start) == "prepare"   # fresh CV, applying off
    assert apply.on_apply_tap(e, "A:1", SHA, True, start) == "prepare"    # fresh CV, applying on + queued
    assert started == [("A:1", "apply"), ("A:1", "apply")]


def test_board_calls_the_hook_on_apply_taps_only(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"A:1": entry(decisions=[])}), encoding="utf-8")
    seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(
        sf, ["127.0.0.1"], "", lambda uid, e: seen.append(uid) or "redraft"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def tap(d):
        req = urllib.request.Request(base + "/api/decide", data=json.dumps({"uid": "A:1", "decision": d}).encode(),
                                     method="POST", headers={"Content-Type": "application/json", "X-RoleGenie": "1"})
        return json.loads(urllib.request.urlopen(req).read())
    try:
        assert tap("consider")["started"] == "none"
        assert tap("apply")["started"] == "redraft"
        assert seen == ["A:1"]
    finally:
        srv.shutdown()


def test_brief_puts_held_failed_and_unconfirmed_first():
    state = {"A:1": entry(applications=[{"status": "held", "need": "Sign in to Workday", "at": NOW}]),
             "A:2": entry(title="Old", applications=[{"status": "submitted", "at": "2026-09-20T00:00:00"}])}
    text, counts = brief.build(state, NOW, "http://x/", NOW)
    needs = text[text.index("Needs you"):text.index("New since")]
    assert "held: Sign in to Workday" in needs and "no confirmation email yet" in needs and counts["needs"] == 2
