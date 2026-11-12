"""RG-S02 acceptance: the local board and locked state writes (CR-rg-s02)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import board
import rolegenie


@pytest.fixture
def state_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "A:1": {"uid": "A:1", "board": "A", "title": "Architect", "location": "London",
                "url": "https://x/1", "score": 8, "verdict": "good", "first_seen": "2026-09-27T07:00:00"},
        "B:2": {"uid": "B:2", "board": "B", "title": "Junior dev", "location": "Leeds",
                "url": "https://x/2", "score": 2, "verdict": "no", "first_seen": "2026-09-27T07:00:00",
                "decisions": [{"decision": "ignored", "at": "2026-09-20T10:00:00", "note": ""}]},
    }), encoding="utf-8")
    return p


@pytest.fixture
def server(state_file):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(state_file, ["127.0.0.1"]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "X-RoleGenie": "1"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers["Content-Type"] else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_lists_jobs_best_score_first(server):
    code, body = call(server + "/api/jobs")
    assert code == 200
    assert [j["uid"] for j in body["jobs"]] == ["A:1", "B:2"]
    assert body["jobs"][0]["band"] == "apply" and body["jobs"][1]["decision"] == "ignored"


def test_serves_the_page(server):
    code, body = call(server + "/")
    assert code == 200 and b"RoleGenie board" in body


def test_apply_is_appended_to_state(server, state_file):
    code, body = call(server + "/api/decide", {"uid": "A:1", "decision": "apply"})
    assert code == 200
    d = json.loads(state_file.read_text())["A:1"]["decisions"]
    assert [x["decision"] for x in d] == ["apply"] and d[0]["via"] == "board"


def test_reject_needs_a_reason(server, state_file):
    code, body = call(server + "/api/decide", {"uid": "A:1", "decision": "reject", "reason": "  "})
    assert code == 400 and "reason" in body["error"]
    assert "decisions" not in json.loads(state_file.read_text())["A:1"]
    code, _ = call(server + "/api/decide", {"uid": "A:1", "decision": "reject", "reason": "Too junior"})
    assert code == 200
    assert json.loads(state_file.read_text())["A:1"]["decisions"][-1]["note"] == "Too junior"


def test_history_is_kept(server, state_file):
    call(server + "/api/decide", {"uid": "B:2", "decision": "consider"})
    d = json.loads(state_file.read_text())["B:2"]["decisions"]
    assert [x["decision"] for x in d] == ["ignored", "consider"]


@pytest.mark.parametrize("body", [{"uid": "Z:9", "decision": "apply"}, {"uid": "A:1", "decision": "applied"}])
def test_bad_requests_are_refused(server, body):
    assert call(server + "/api/decide", body)[0] == 400


def test_write_without_header_is_refused(server, state_file):
    code, _ = call(server + "/api/decide", {"uid": "A:1", "decision": "apply"}, {"X-RoleGenie": "0"})
    assert code == 403
    assert "decisions" not in json.loads(state_file.read_text())["A:1"]


def test_unknown_host_is_refused(server):
    assert call(server + "/api/jobs", headers={"Host": "evil.example"})[0] == 421


@pytest.mark.parametrize("host,ok", [("127.0.0.1:8765", True), ("LOCALHOST", True),
                                     ("devgenie:8765", True), ("evil.example", False), ("", False)])
def test_allowed_host_mirrors_contract(host, ok):
    assert board.allowed_host(host, ["127.0.0.1", "localhost", "devgenie"]) is ok


def test_run_save_keeps_a_decision_made_mid_run(state_file):
    """INV-RG-02: the nightly run re-reads state under the lock before saving."""
    stale = rolegenie.load_state(state_file)          # run loads state at start
    board.decide(state_file, "A:1", "apply", "")      # owner taps during the run
    rolegenie.update_state(state_file, lambda st: st.setdefault(
        "C:3", {"uid": "C:3", "title": "New"}))       # run saves its new postings
    after = rolegenie.load_state(state_file)
    assert "decisions" not in stale["A:1"]
    assert after["A:1"]["decisions"][-1]["decision"] == "apply" and "C:3" in after


def test_lock_blocks_a_second_writer(state_file):
    with rolegenie.state_lock(state_file):
        with pytest.raises(TimeoutError):
            with rolegenie.state_lock(state_file, timeout=0.3):
                pass
    with rolegenie.state_lock(state_file, timeout=0.3):
        pass


def test_stale_lock_is_cleared(state_file, monkeypatch):
    lock = state_file.with_suffix(".json.lock")
    lock.write_text("")
    monkeypatch.setattr(rolegenie, "STALE_LOCK_SECONDS", -1)
    with rolegenie.state_lock(state_file, timeout=0.3):
        pass
    assert not lock.exists()
