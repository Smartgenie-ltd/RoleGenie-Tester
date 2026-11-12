"""BRIEF-038 (CR-rg-tester-command-boundary): the overnight daily workflow, the submission boundary, the apply
intent / human applied mark, and the scheduler command. Literal cases mirror tests/contract/daily-workflow.test.mjs.
Synthetic data only; every test runs in a temporary ROLEGENIE_HOME."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apply                      # noqa: E402
import board                      # noqa: E402
import onboard                    # noqa: E402
import outcomes                   # noqa: E402
import rolegenie                  # noqa: E402
import tailor                     # noqa: E402

STEPS = ["import-decisions", "scan", "assess-pending", "apply-intents", "draft-cvs", "mail-review", "push-board",
         "report"]


@pytest.fixture(autouse=True)
def _restore_approved_sha():
    orig = tailor.approved_sha
    yield
    tailor.approved_sha = orig


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ROLEGENIE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def cfg(home):
    return {"output": {"state_file": str(home / "state.json"), "digest_dir": str(home / "digests")},
            "tailor": {"jobs_dir": str(home / "jobs")}, "apply": {"enabled": True}}


def approve(home):
    (home / "master.json").write_text(json.dumps({"facts": []}), encoding="utf-8")
    tailor.approved_sha = lambda: "sha1"       # restored by the autouse fixture below


def write_state(cfg, state):
    Path(cfg["output"]["state_file"]).write_text(json.dumps(state), encoding="utf-8")


def read_state(cfg):
    return json.loads(Path(cfg["output"]["state_file"]).read_text(encoding="utf-8"))


def ok_steps(calls):
    def mk(name):
        def f(cfg):
            calls.append(name)
            return ""
        return f
    return {n: mk(n) for n in STEPS if n != "report"}


# ---- refusal, order, isolation -------------------------------------------------------------------------------------

def test_no_approved_master_refuses_before_any_step(home, cfg):
    calls = []
    rep = rolegenie.daily_run(cfg, steps=ok_steps(calls))
    assert rep["status"] == "refused" and "onboard" in rep["reason"]
    assert calls == []


def test_steps_run_in_order_and_report_is_written(home, cfg):
    approve(home)
    calls = []
    rep = rolegenie.daily_run(cfg, steps=ok_steps(calls))
    assert calls == STEPS[:-1]
    assert [r["step"] for r in rep["steps"]] == STEPS
    assert rep["status"] == "ok"
    saved = json.loads((home / "daily-report.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok" and saved["finishedAt"]


def test_one_failing_step_does_not_stop_the_rest_and_is_retried_once(home, cfg):
    approve(home)
    calls = []
    steps = ok_steps(calls)

    def boom(cfg):
        calls.append("assess-pending")
        raise RuntimeError("network down")
    steps["assess-pending"] = boom
    rep = rolegenie.daily_run(cfg, steps=steps)
    assert calls.count("assess-pending") == 2            # one retry, bounded
    by = {r["step"]: r for r in rep["steps"]}
    assert by["assess-pending"]["status"] == "failed" and "network down" in by["assess-pending"]["reason"]
    assert by["scan"]["status"] == "ok" and by["draft-cvs"]["status"] == "ok"
    assert rep["status"] == "partial"


def test_optional_step_not_set_up_is_skipped_with_its_reason(home, cfg):
    approve(home)
    steps = ok_steps([])

    def none(cfg):
        raise rolegenie.StepSkipped("the phone board is not set up")
    steps["push-board"] = none
    rep = rolegenie.daily_run(cfg, steps=steps)
    by = {r["step"]: r for r in rep["steps"]}
    assert by["push-board"] == {"step": "push-board", "status": "skipped", "reason": "the phone board is not set up"}
    assert rep["status"] == "ok"


def test_default_board_and_mail_steps_skip_when_not_set_up(home, cfg, monkeypatch):
    approve(home)
    monkeypatch.setattr(rolegenie, "cmd_scan", lambda args, cfg: 0)
    monkeypatch.setattr(rolegenie, "assess_pending", lambda cfg, limit=30: {})
    monkeypatch.setattr(tailor, "cmd_tailor", lambda argv, cfg: 0)
    write_state(cfg, {})
    rep = rolegenie.daily_run(cfg)
    by = {r["step"]: r for r in rep["steps"]}
    assert by["import-decisions"]["status"] == "skipped" and "not set up" in by["import-decisions"]["reason"]
    assert by["push-board"]["status"] == "skipped"
    assert by["mail-review"]["status"] == "skipped"
    assert rep["status"] == "ok"


# ---- overlap and honest missed runs ----------------------------------------------------------------------------------

def test_live_lock_refuses_a_second_run_and_stale_lock_is_taken_over(home, cfg):
    approve(home)
    calls = []
    (home / "daily.lock").write_text(json.dumps({"pid": os.getpid(), "startedAt": rolegenie.hours_now() - 1}))
    rep = rolegenie.daily_run(cfg, steps=ok_steps(calls))
    assert rep["status"] == "refused" and "another" in rep["reason"] and calls == []
    (home / "daily.lock").write_text(json.dumps({"pid": os.getpid(), "startedAt": rolegenie.hours_now() - 7}))
    rep = rolegenie.daily_run(cfg, steps=ok_steps(calls))
    assert rep["status"] == "ok"
    assert not (home / "daily.lock").exists()             # released


def test_lock_is_released_when_the_run_dies_unexpectedly(home, cfg, monkeypatch):
    approve(home)
    monkeypatch.setattr(onboard, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        rolegenie.daily_run(cfg, steps=ok_steps([]))
    assert not (home / "daily.lock").exists()


def test_lock_is_taken_atomically_and_a_foreign_lock_is_not_removed(home):
    assert rolegenie._take_daily_lock() is True
    assert rolegenie._take_daily_lock() is False          # live, young: refused
    (home / "daily.lock").write_text(json.dumps({"pid": os.getpid() + 1, "startedAt": rolegenie.hours_now()}))
    rolegenie._release_daily_lock()                       # not ours: left alone
    assert (home / "daily.lock").exists()


def test_a_refused_run_leaves_a_trace_in_the_log(home, cfg, capsys):
    assert rolegenie.cmd_daily([], cfg) == 1               # no approved master
    assert "not started" in (home / "daily.log").read_text(encoding="utf-8")


def test_status_text_says_never_run_and_missed(home, cfg, capsys):
    assert "never" in rolegenie.daily_status_text().lower()
    (home / "daily-report.json").write_text(json.dumps(
        {"status": "ok", "finishedHours": rolegenie.hours_now() - 60, "finishedAt": "2026-11-01T07:00:00", "steps": []}))
    text = rolegenie.daily_status_text()
    assert "missed" in text.lower() and "off or asleep" in text


# ---- scheduler command ------------------------------------------------------------------------------------------------

def test_scheduler_commands_quote_paths_with_spaces():
    lin = rolegenie.scheduler_command("linux", "/home/a b/rg/.venv/bin/python", "/home/a b/rg", "/home/a b/.rolegenie/daily.log")
    assert lin == ("0 7 * * * cd '/home/a b/rg' && '/home/a b/rg/.venv/bin/python' rolegenie.py daily")
    win = rolegenie.scheduler_command("win32", "C:\\Users\\A B\\rg\\.venv\\Scripts\\python.exe", "C:\\Users\\A B\\rg",
                                      "C:\\Users\\A B\\daily.log")
    assert win == ('schtasks /Create /SC DAILY /ST 07:00 /TN RoleGenie /TR "\\"C:\\Users\\A B\\rg\\.venv\\Scripts\\python.exe\\" '
                   '\\"C:\\Users\\A B\\rg\\rolegenie.py\\" daily"')
    assert "cmd /c" not in win and "&&" not in win and ">>" not in win


# ---- the submission boundary -------------------------------------------------------------------------------------------

def test_tester_workflow_never_submits_even_with_the_legacy_switch(home, cfg, monkeypatch):
    approve(home)
    called = []
    monkeypatch.setattr(apply, "run_applier", lambda p: called.append(p) or {"status": "submitted", "need": "", "confirmation": ""})
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "https://x/1",
                              "decisions": [{"decision": "apply", "at": "2026-11-06T07:00:00"}]}})
    monkeypatch.setattr(tailor, "current_master_sha", lambda cfg: "legacy")
    monkeypatch.setattr(apply, "_apply_locked", lambda uid, cfg: called.append("locked"))
    assert apply.submission_allowed(cfg) is False
    assert apply.apply_one("x:1", cfg) is None             # direct command call refuses before lock/network/model
    assert called == []
    assert not list(ROOT.glob(".applying-*.lock"))


def test_legacy_flow_without_a_master_is_unchanged(home, cfg):
    assert apply.submission_allowed(cfg) is True
    assert apply.submission_allowed({"apply": {"enabled": False}}) is False


def test_tester_package_is_never_auto_submit(home, cfg, monkeypatch):
    approve(home)
    monkeypatch.setattr(tailor, "approved_sha", lambda: "sha1")
    monkeypatch.setattr(apply, "fetch_form_questions", lambda e, cfg: ([], "none"))
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "https://boards.greenhouse.io/x/jobs/1",
                              "decisions": [{"decision": "apply", "at": "2026-11-06T07:00:00"}],
                              "cv": {"status": "drafted", "pdf": "p.pdf", "master_sha": "sha1", "masterSha": "sha1"}}})
    pkg = apply.prepare_package("x:1", cfg)
    assert pkg["auto_submit"] is False and pkg["master_sha"] == "sha1"


def test_tester_apply_tap_uses_the_approved_master_and_does_not_redraft_a_current_cv(home, cfg, monkeypatch):
    approve(home)
    monkeypatch.setattr(tailor, "approved_sha", lambda: "sha1")
    started = []
    entry = {"decisions": [{"decision": "apply", "at": "t"}],
             "cv": {"status": "drafted", "master_sha": "sha1", "masterSha": "sha1"}}
    assert apply.on_apply_tap(entry, "x:1", "legacy-sha", False, start=lambda u, c="apply": started.append(c)) == "kept"
    assert started == []
    stale = {**entry, "cv": {"status": "drafted", "master_sha": "old", "masterSha": "old"}}
    assert apply.on_apply_tap(stale, "x:1", "legacy-sha", False, start=lambda u, c="apply": started.append(c)) == "draft"
    assert started == ["tailor"]
    # a held approved master (None) starts nothing
    monkeypatch.setattr(tailor, "approved_sha", lambda: None)
    started.clear()
    assert apply.on_apply_tap(stale, "x:1", "legacy-sha", False, start=lambda u, c="apply": started.append(c)) == "held"
    assert started == []


# ---- intent vs the human applied mark ------------------------------------------------------------------------------------

def test_board_apply_is_an_intent_and_applied_is_the_human_mark_once(home, cfg):
    approve(home)
    st = Path(cfg["output"]["state_file"])
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "cv": {"master_sha": "sha1"}}})
    e = board.decide(st, "x:1", "apply", "good fit")
    assert outcomes.submission_status(e["application"]) == "intent"
    e = board.decide(st, "x:1", "applied-externally", "")
    app = e["application"]
    assert outcomes.submission_status(app) == "submitted" and app["cvRef"] == "sha1"
    first = [h for h in app["history"] if h["evidence"] == "human-applied"]
    assert len(first) == 1
    e = board.decide(st, "x:1", "applied-externally", "")
    assert [h for h in e["application"]["history"] if h["evidence"] == "human-applied"] == first   # exactly once


def test_mark_apply_is_intent_and_mark_applied_confirms(home, cfg, capsys):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "cv": {"master_sha": "sha1"}}})
    assert rolegenie.cmd_mark(["x:1", "apply", "--note", "why"], cfg) == 0
    assert outcomes.submission_status(read_state(cfg)["x:1"]["application"]) == "intent"
    assert rolegenie.cmd_mark(["x:1", "applied"], cfg) == 0
    app = read_state(cfg)["x:1"]["application"]
    assert outcomes.submission_status(app) == "submitted"
    n = len(app["history"])
    assert rolegenie.cmd_mark(["x:1", "applied"], cfg) == 0
    assert len(read_state(cfg)["x:1"]["application"]["history"]) == n


def test_imported_apply_decisions_get_one_intent_and_a_repeat_adds_nothing(home, cfg):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u",
                              "decisions": [{"decision": "apply", "at": "2026-11-06T07:00:00"}]},
                      "x:2": {"board": "X", "title": "R", "url": "u2",
                              "decisions": [{"decision": "reject", "at": "2026-11-06T07:00:00"}]}})
    assert rolegenie.record_apply_intents(cfg) == ["x:1"]
    assert rolegenie.record_apply_intents(cfg) == []
    s = read_state(cfg)
    assert outcomes.submission_status(s["x:1"]["application"]) == "intent" and "application" not in s["x:2"]


# ---- journey: decisions -> intent -> draft -> status, repeat is idempotent ----------------------------------------------

def test_journey_apply_gets_one_draft_and_a_board_status_and_a_repeat_changes_nothing(home, cfg, monkeypatch):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u",
                              "decisions": [{"decision": "apply", "at": "2026-11-06T07:00:00"}]}})
    drafts = []

    def fake_tailor(argv, cfg):
        s = read_state(cfg)
        for uid, e in s.items():
            if (e.get("cv") or {}).get("status") != "drafted":
                drafts.append(uid)
                e["cv"] = {"status": "drafted", "master_sha": "sha1", "masterSha": "sha1", "pdf": "p.pdf"}
        write_state(cfg, s)
        return 0
    monkeypatch.setattr(tailor, "cmd_tailor", fake_tailor)
    monkeypatch.setattr(rolegenie, "cmd_scan", lambda args, cfg: 0)
    monkeypatch.setattr(rolegenie, "assess_pending", lambda cfg, limit=30: {})
    onboard.save("board-identity.json", {"version": 1, "links": {"b1": "x:1"}, "ambiguous": {}})
    pushed = []
    import board_sheet
    monkeypatch.setattr(board_sheet, "_config", lambda need_url=True: {"url": "u", "secret": "s"})
    monkeypatch.setattr(board_sheet, "pull_decisions", lambda cfg: {"imported": [], "held": [], "alreadyKnown": 0})
    monkeypatch.setattr(board_sheet, "cmd_push", lambda cfg=None: pushed.append(onboard.load("board-docs.json", {})) or 0)
    rep1 = rolegenie.daily_run(cfg)
    rep2 = rolegenie.daily_run(cfg)
    assert rep1["status"] == "ok" and rep2["status"] == "ok"
    assert drafts == ["x:1"]                                # one draft only
    s = read_state(cfg)
    assert outcomes.submission_status(s["x:1"]["application"]) == "intent"      # intent alone is not a submission
    assert len(s["x:1"]["decisions"]) == 1
    assert pushed[0]["b1"]["cvStatus"] == "draft"          # the phone board shows the draft status


def test_board_status_shows_held_with_reason_and_keeps_a_supplied_https_link(home, cfg):
    write_state(cfg, {"x:1": {"cv": {"status": "blocked", "reason": "master changed"}}, "x:2": {"cv": {"status": "drafted"}}})
    onboard.save("board-identity.json", {"version": 1, "links": {"b1": "x:1", "b2": "x:2"}, "ambiguous": {}})
    onboard.save("board-docs.json", {"b2": {"cvStatus": "draft", "docLink": "https://example.test/doc", "docNote": ""}})
    rolegenie.project_cv_status(cfg)
    docs = onboard.load("board-docs.json", {})
    assert docs["b1"] == {"cvStatus": "held", "docLink": "", "docNote": "master changed"}
    assert docs["b2"]["docLink"] == "https://example.test/doc" and docs["b2"]["cvStatus"] == "draft"


# ---- preference ordering, mail, env ---------------------------------------------------------------------------------------

def test_new_postings_are_ordered_by_preference_without_touching_fit():
    state = {"s:1": {"title": "Python Developer", "board": "B", "url": "u0",
                     "decisions": [{"decision": "apply", "at": "2026-11-01T00:00:00", "note": "python"}]}}
    a = rolegenie.Posting(uid="a:1", board="A", title="Java Engineer", location="L", url="u1", body="x")
    b = rolegenie.Posting(uid="a:2", board="A", title="Python Engineer", location="L", url="u2", body="x")
    a.score, b.score = 9, 6
    ordered = rolegenie.order_by_preference([a, b], state)
    assert [p.uid for p in ordered] == ["a:2", "a:1"]
    assert (a.score, b.score) == (9, 6)                    # ordering only: fit never changes


def test_mail_review_queues_proposals_and_confirm_needs_the_person(home, cfg, monkeypatch):
    approve(home)
    write_state(cfg, {"x:1": {"board": "Acme", "title": "Engineer", "url": "u",
                              "decisions": [{"decision": "apply", "at": "2026-11-01T00:00:00"}],
                              "application": outcomes.confirm_applied(
                                  outcomes.new_application("manual", "sha1", False, "2026-11-01T00:00:00", intent=True),
                                  "2026-11-01T00:00:00")}})
    msg = {"id": "m1", "sender": "jobs@acme.example", "subject": "Interview for Engineer at Acme",
           "body": "We would like to invite you to interview for the Engineer role at Acme.", "date": "2026-11-05"}
    monkeypatch.setattr(rolegenie, "mail_runner", lambda cfg: (lambda: json.dumps({"messages": [msg]})))
    monkeypatch.setattr(outcomes, "jev_classify_email", lambda *a, **k: {"label": "interview-invite", "confidence": 0.9})
    rolegenie.mail_review_step({**cfg, "mail": {"enabled": True}})
    ledger = outcomes.load_ledger(home / "mail-ledger.json")
    assert ledger["proposals"], "a proposal is queued for review"
    pid = next(iter(ledger["proposals"]))
    assert read_state(cfg)["x:1"]["application"]["outcome"] == "open"        # nothing applied without the person
    assert rolegenie.cmd_mail(["review"], cfg) == 0


def test_every_direct_claude_launch_uses_the_subscription_auth_env(home, cfg, monkeypatch):
    seen = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(apply.shutil, "which", lambda n: "/usr/bin/claude")

    class P:
        stdout = '{"status": "submitted", "need": "", "confirmation": ""}'
        returncode = 0
    monkeypatch.setattr(apply.subprocess, "run", lambda argv, **k: seen.append(k.get("env")) or P())
    apply.run_applier("p")
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "applications": [{"status": "submitted", "at": "2026-11-01T00:00:00"}]}})
    apply.verify(cfg)
    import brief
    assert seen and all(env is not None and "ANTHROPIC_API_KEY" not in env for env in seen)
    assert "env=rolegenie._model_env" in (ROOT / "brief.py").read_text(encoding="utf-8")


# ---- inventory ------------------------------------------------------------------------------------------------------------------

def test_inventory_lists_every_reachable_product_module_prompt_and_asset():
    import ast
    inv = (ROOT / "docs/audit/evidence/tester-distribution-inventory-2026-11-05.md").read_text(encoding="utf-8")
    local = {p.stem for p in ROOT.glob("*.py")}
    seen, todo = set(), ["rolegenie", "apply", "board", "brief", "first_run_check"]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        for node in ast.walk(ast.parse((ROOT / f"{m}.py").read_text(encoding="utf-8"))):
            names = ([a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import)
                     else [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else [])
            todo += [n for n in names if n in local]
    for m in sorted(seen):
        assert f"`{m}.py`" in inv, f"{m}.py missing from the distribution inventory"
    for item in ("prompts/", "assets/fonts/", "apps-script/Code.gs", "apps-script/Page.html", "config.example.yaml",
                 "ats_rules.yaml", "requirements.txt", ".env.example", "scorers/", "tests/test_daily_workflow.py"):
        assert item in inv, f"{item} missing from the distribution inventory"
    assert not any(m in seen for m in ("gate",)), "product code must not import the harness"


# ---- review findings (dry run GAP, 2026-11-06) ----------------------------------------------------------------------------

def test_legacy_mark_and_board_records_are_unchanged_without_a_master(home, cfg):
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u"}})
    board.decide(Path(cfg["output"]["state_file"]), "x:1", "apply", "r")
    app = read_state(cfg)["x:1"]["application"]
    assert "submission" not in app and len(app["history"]) == 1


def test_imported_already_applied_gets_the_human_mark_at_its_time_with_the_cv_in_force(home, cfg):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "cv": {"masterSha": "sha1", "master_sha": "sha1"},
                              "decisions": [{"decision": "apply", "at": "2026-11-05T07:00:00"},
                                            {"decision": "applied", "at": "2026-11-05T19:30:00",
                                             "boardDecision": "applied-externally"}]}})
    assert rolegenie.record_apply_intents(cfg) == ["x:1"]
    app = read_state(cfg)["x:1"]["application"]
    assert outcomes.submission_status(app) == "submitted" and app["cvRef"] == "sha1"
    assert [h["at"] for h in app["history"] if h["evidence"] == "human-applied"] == ["2026-11-05T19:30:00"]
    assert rolegenie.record_apply_intents(cfg) == []


def test_intent_cv_reference_is_filled_at_the_human_mark_when_the_tap_preceded_the_draft(home, cfg):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u"}})
    board.decide(Path(cfg["output"]["state_file"]), "x:1", "apply", "r")          # no CV yet: cvRef None
    s = read_state(cfg)
    s["x:1"]["cv"] = {"masterSha": "sha1", "master_sha": "sha1"}
    write_state(cfg, s)
    board.decide(Path(cfg["output"]["state_file"]), "x:1", "applied-externally", "")
    assert read_state(cfg)["x:1"]["application"]["cvRef"] == "sha1"


def test_scan_and_mail_are_not_retried_but_idempotent_steps_are(home, cfg):
    approve(home)
    calls = []
    steps = ok_steps(calls)
    for name in ("scan", "mail-review", "draft-cvs"):
        steps[name] = (lambda n: lambda cfg: calls.append(n) or (_ for _ in ()).throw(RuntimeError("x")))(name)
    rolegenie.daily_run(cfg, steps=steps)
    assert calls.count("scan") == 1 and calls.count("mail-review") == 1 and calls.count("draft-cvs") == 2


def test_stale_approved_master_refuses_and_a_corrupt_master_never_submits(home, cfg):
    approve(home)
    tailor.approved_sha = lambda: None
    assert rolegenie.daily_run(cfg, steps=ok_steps([]))["status"] == "refused"
    (home / "master.json").write_text("{not json", encoding="utf-8")
    assert apply.submission_allowed(cfg) is False          # fails closed: never True with a master file present


def test_push_with_nothing_to_push_is_skipped_not_failed(home, cfg, monkeypatch):
    import board_sheet
    monkeypatch.setattr(board_sheet, "_config", lambda need_url=True: {"url": "u", "secret": "s"})
    monkeypatch.setattr(board_sheet, "cmd_push", lambda cfg=None: 1)
    with pytest.raises(rolegenie.StepSkipped):
        rolegenie._step_push_board(cfg)


def test_windows_lock_liveness_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("would terminate on Windows")))
    assert rolegenie._pid_alive(12345) is True


def test_daily_scan_does_not_read_the_command_line(home, cfg, monkeypatch):
    approve(home)
    monkeypatch.setattr(sys, "argv", ["rolegenie.py", "daily"])
    seen = []
    monkeypatch.setattr(rolegenie, "fetch_all", lambda boards: seen.append(1) or ([], []))
    monkeypatch.setattr(rolegenie, "load_state", lambda p: {})
    cfg2 = {**cfg, "boards": {}, "matching": {}}
    try:
        rolegenie._step_scan(cfg2)
    except SystemExit:
        pytest.fail("the scan parsed the command line")
    except Exception:
        pass
    assert seen, "the scan did not run"


def test_first_run_check_reports_daily_prerequisites_without_failing_the_install(home, capsys, monkeypatch):
    import first_run_check
    rows = list(first_run_check.check_workflow_setup())
    assert any("approved master" in r[1] and not r[0] for r in rows)
    assert all(len(r) == 4 and r[3] == "optional" for r in rows)       # next steps, never an install failure
    (home / "master.json").write_text("{}", encoding="utf-8")
    assert any("approved master" in r[1] and r[0] for r in first_run_check.check_workflow_setup())


def test_legacy_ambiguous_old_apply_taps_are_never_claimed_as_submissions(home, cfg):
    approve(home)
    legacy = outcomes.new_application("manual", None, False, "2026-10-01T00:00:00")          # no intent marker
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "application": legacy,
                              "decisions": [{"decision": "applied", "at": "2026-10-01T00:00:00"}]}})
    assert rolegenie.record_apply_intents(cfg) == []
    assert outcomes.submission_status(read_state(cfg)["x:1"]["application"]) == "legacy-ambiguous"


def test_human_mark_takes_the_cv_in_force_not_the_one_at_the_tap(home, cfg):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "cv": {"masterSha": "old", "master_sha": "old"}}})
    board.decide(Path(cfg["output"]["state_file"]), "x:1", "apply", "r")
    s = read_state(cfg)
    s["x:1"]["cv"] = {"masterSha": "sha1", "master_sha": "sha1"}
    write_state(cfg, s)
    board.decide(Path(cfg["output"]["state_file"]), "x:1", "applied-externally", "")
    assert read_state(cfg)["x:1"]["application"]["cvRef"] == "sha1"


def test_mail_matching_uses_submitted_records_only(home, cfg, monkeypatch):
    approve(home)
    intent = outcomes.new_application("manual", "s", False, "2026-11-01T00:00:00", intent=True)
    write_state(cfg, {"x:1": {"board": "Acme", "title": "Engineer", "application": intent}})
    seen = []
    monkeypatch.setattr(rolegenie, "mail_runner", lambda cfg: (lambda: json.dumps({"messages": []})))
    monkeypatch.setattr(outcomes, "read_threads", lambda r: seen.append(1) or {"messages": [], "problems": []})
    rolegenie.mail_review_step({**cfg, "mail": {"enabled": True}})
    monkeypatch.setattr(outcomes, "propose_outcome", lambda m, apps, c: seen.append(apps) or {"id": "p"})
    monkeypatch.setattr(outcomes, "read_threads", lambda r: {"messages": [{"id": "m"}], "problems": []})
    rolegenie.mail_review_step({**cfg, "mail": {"enabled": True}})
    assert [] in seen                                      # the intent-only application is not a match candidate


def test_legacy_scan_order_is_not_changed_by_preference_without_a_master(home, cfg, monkeypatch):
    called = []
    monkeypatch.setattr(rolegenie, "order_by_preference", lambda p, s: called.append(1) or p)
    monkeypatch.setattr(rolegenie, "fetch_all", lambda boards: ([], []))
    monkeypatch.setattr(rolegenie, "update_state", lambda *a, **k: None)
    cfg2 = {**cfg, "boards": {}, "matching": {}, "scoring": {}, "email": {}}
    try:
        rolegenie.cmd_scan(__import__("argparse").Namespace(dry_run=True, verify=False, backfill=False), cfg2)
    except Exception:
        pass
    assert called == []                                    # no approved master: legacy order untouched


def test_run_applier_refuses_with_a_master_before_any_subprocess(home, monkeypatch):
    approve(home)
    monkeypatch.setattr(apply.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched")))
    assert apply.run_applier("p")["status"] == "held"



def test_phone_already_applied_is_claimed_even_over_a_legacy_record_but_a_raw_old_applied_is_not(home, cfg):
    approve(home)
    legacy = outcomes.new_application("manual", None, False, "2026-10-01T00:00:00")
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u", "application": legacy,
                              "decisions": [{"decision": "applied", "at": "2026-11-05T19:30:00",
                                             "boardDecision": "applied-externally"}]},
                      "x:2": {"board": "X", "title": "T2", "url": "u2",
                              "decisions": [{"decision": "applied", "at": "2026-09-01T00:00:00"}]}})
    assert rolegenie.record_apply_intents(cfg) == ["x:1"]
    assert "application" not in read_state(cfg)["x:2"]


def test_an_empty_young_lock_is_a_run_in_progress_not_a_stale_lock(home):
    (home / "daily.lock").write_text("")
    assert rolegenie._take_daily_lock() is False
    old = rolegenie.time.time() - 300
    os.utime(home / "daily.lock", (old, old))
    assert rolegenie._take_daily_lock() is True


def test_first_run_mail_line_follows_the_config_in_either_yaml_style(home):
    import first_run_check
    mail = lambda: next(r for r in first_run_check.check_workflow_setup() if "mail" in r[1])[0]  # noqa: E731
    assert mail() is False
    (home / "config.yaml").write_text("mail: {enabled: true}\n", encoding="utf-8")
    assert mail() is True
    (home / "config.yaml").write_text("mail:\n  enabled: true\n", encoding="utf-8")
    assert mail() is True


def test_push_board_reprojects_after_the_push_links_new_jobs(home, cfg, monkeypatch):
    import board_sheet
    write_state(cfg, {"x:1": {"cv": {"status": "drafted"}}})
    pushes = []

    def push(cfg=None):
        pushes.append(1)
        onboard.save("board-identity.json", {"version": 1, "links": {"b1": "x:1"}, "ambiguous": {}})
        return 0
    monkeypatch.setattr(board_sheet, "_config", lambda need_url=True: {"url": "u", "secret": "s"})
    monkeypatch.setattr(board_sheet, "cmd_push", push)
    rolegenie._step_push_board(cfg)
    assert len(pushes) == 2 and onboard.load("board-docs.json", {})["b1"]["cvStatus"] == "draft"


# ---- attempt 2: acceptance run 1 must-fix items (each written to fail first) ------------------------------------------------

def test_m1_funnel_counts_only_human_confirmed_submissions_and_lapse_skips_intents(home, cfg, capsys):
    intent = outcomes.new_application("site", None, False, now="2026-10-01T09:00:00", intent=True)
    human = outcomes.confirm_applied(
        outcomes.new_application("site", None, False, now="2026-10-01T09:00:00", intent=True), "2026-10-02T09:00:00")
    human = dict(human, appliedAt="2026-10-02T09:00:00")
    f = outcomes.funnel_counts({"a": {"score": 70, "application": intent}, "b": {"score": 70, "application": human}},
                               above_bar=lambda e: True)
    assert f["applied"] == 1 and f["open"] == 1 and f["intent_only"] == 1
    assert f["interviews_per_application"] == 0.0
    old = outcomes.new_application("site", None, False, now="2020-01-01T09:00:00", intent=True)
    write_state(cfg, {"x:1": {"title": "T", "application": old}})
    assert rolegenie.cmd_lapse(["--days", "28"], cfg) == 0
    assert read_state(cfg)["x:1"]["application"]["outcome"] == "open"           # an intent never lapses


def test_m1_funnel_command_shows_intent_only_separately(home, cfg, capsys):
    intent = outcomes.new_application("site", None, False, now="2026-10-01T09:00:00", intent=True)
    write_state(cfg, {"x:1": {"score": 70, "application": intent}})
    rolegenie.cmd_funnel([], cfg)
    out = capsys.readouterr().out
    assert "applied         0" in out and "intent only" in out


def test_m2_draft_and_assess_failures_are_reported_failed_with_counts(home, cfg, monkeypatch):
    approve(home)
    write_state(cfg, {"x:1": {"decisions": [{"decision": "apply", "at": "t"}]},
                      "x:2": {"decisions": [{"decision": "apply", "at": "t"}]}})

    def tailor_some(argv, cfg):
        s = read_state(cfg)
        s["x:1"]["cv"] = {"status": "drafted", "masterSha": "sha1", "master_sha": "sha1"}
        s["x:2"]["cv"] = {"status": "failed", "problems": ["the drafting model failed"]}
        write_state(cfg, s)
        return 0
    monkeypatch.setattr(tailor, "cmd_tailor", tailor_some)
    with pytest.raises(rolegenie.StepFailed) as exc:
        rolegenie._step_draft_cvs(cfg)
    assert "1 drafted" in str(exc.value) and "1 failed" in str(exc.value)
    monkeypatch.setattr(rolegenie, "assess_pending", lambda cfg, limit=30: {"assessed": 2, "failed": 1})
    with pytest.raises(rolegenie.StepFailed) as exc:
        rolegenie._step_assess_pending(cfg)
    assert "failed 1" in str(exc.value) and "assessed 2" in str(exc.value)
    monkeypatch.setattr(rolegenie, "assess_pending", lambda cfg, limit=30: {"assessed": 2})
    assert "assessed 2" in rolegenie._step_assess_pending(cfg)


def test_m2_a_reported_step_failure_is_not_retried(home, cfg):
    approve(home)
    calls = []
    steps = ok_steps(calls)

    def failing(cfg):
        calls.append("draft-cvs")
        raise rolegenie.StepFailed("1 drafted, 1 failed")
    steps["draft-cvs"] = failing
    rep = rolegenie.daily_run(cfg, steps=steps)
    assert calls.count("draft-cvs") == 1
    assert {r["step"]: r for r in rep["steps"]}["draft-cvs"]["status"] == "failed"


def test_m3_board_approve_refuses_and_records_nothing_in_approved_mode(home, cfg, monkeypatch):
    approve(home)
    started = []
    monkeypatch.setattr(apply, "start_in_background", lambda uid, command="apply": started.append(uid))
    st = Path(cfg["output"]["state_file"])
    write_state(cfg, {"x:1": {"decisions": [{"decision": "apply", "at": "t"}],
                              "package": {"status": "ready", "auto_submit": True, "master_sha": "m"}}})
    import io
    from unittest.mock import MagicMock
    body = json.dumps({"uid": "x:1"}).encode()
    handler = board.make_handler(st, ["127.0.0.1"], "", None, lambda: "m", {"apply": {"enabled": True}})
    h = handler.__new__(handler)
    h.headers = {"Content-Type": "application/json", "Content-Length": str(len(body)), board.WRITE_HEADER: "1",
                 "Host": "127.0.0.1"}
    h.path, h.rfile, h._send = "/api/approve", io.BytesIO(body), MagicMock()
    h.client_address = ("127.0.0.1", 1)
    h.do_POST()
    code, out, _ = h._send.call_args.args
    assert code == 400 and "automatic" in json.loads(out)["error"]
    assert "approved" not in read_state(cfg)["x:1"]["package"] and started == []


@pytest.mark.parametrize("content", ["", "null", "{broken", "[]"])
def test_m4_a_present_but_unreadable_master_is_still_approved_mode(home, cfg, content):
    (home / "master.json").write_text(content, encoding="utf-8")
    assert rolegenie.approved_mode() is True
    assert apply.submission_allowed({"apply": {"enabled": True}}) is False
    assert apply.apply_one("x:1", cfg) is None
    assert rolegenie.daily_run(cfg, steps=ok_steps([]))["status"] == "refused"       # not a crash, not legacy


def test_m5_a_run_whose_executed_steps_all_failed_is_failed_skips_excluded(home, cfg):
    approve(home)
    steps = {n: (lambda cfg: (_ for _ in ()).throw(rolegenie.StepFailed("x"))) for n in STEPS if n != "report"}
    for n in ("import-decisions", "mail-review", "push-board"):
        steps[n] = lambda cfg: (_ for _ in ()).throw(rolegenie.StepSkipped("not set up"))
    rep = rolegenie.daily_run(cfg, steps=steps)
    assert rep["status"] == "failed"
    steps["scan"] = lambda cfg: ""
    assert rolegenie.daily_run(cfg, steps=steps)["status"] == "partial"


def test_m6_the_human_mark_time_is_the_applied_time(home, cfg):
    approve(home)
    write_state(cfg, {"x:1": {"board": "X", "title": "T", "url": "u"}})
    st = Path(cfg["output"]["state_file"])
    board.decide(st, "x:1", "apply", "r")
    tap = read_state(cfg)["x:1"]["application"]["appliedAt"]
    s = read_state(cfg)
    s["x:1"]["decisions"].append({"decision": "applied", "at": "2099-01-01T10:00:00", "boardDecision": "applied-externally"})
    write_state(cfg, s)
    rolegenie.record_apply_intents(cfg)
    app = read_state(cfg)["x:1"]["application"]
    assert app["appliedAt"] == "2099-01-01T10:00:00" != tap
