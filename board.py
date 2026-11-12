"""RoleGenie local board (arch.md §10 D1, RG-S02).

A small web app over state.json: it lists postings with their scores and
records the owner's Apply / Consider / Reject calls. It listens only on
localhost and the PC's Tailscale address, answers only to the host names in
config, and refuses a state-changing call without the X-RoleGenie header, so
a web page elsewhere cannot drive it.

    python board.py            # serve on the configured addresses
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any


import rolegenie

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "board.html"
BOARD_DECISIONS = ("apply", "consider", "reject", "applied-externally", "archive")  # CR-rg-applied-externally, CR-rg-archive
WRITE_HEADER = "X-RoleGenie"
MAX_BODY = 16_384
SERVABLE = {"pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def allowed_host(host: str, allowed: list[str]) -> bool:
    """Mirror of contracts/rolegenie/domain.ts isAllowedHost."""
    name = host.strip().lower()
    if ":" in name and not name.startswith("["):
        name = name.rsplit(":", 1)[0]
    return any(a.lower() == name for a in allowed)


def band(score: Any) -> str:
    if not isinstance(score, int):
        return "unscored"
    return "apply" if score >= 7 else "consider" if score >= 4 else "archive"


def cv_summary(e: dict, master_sha: str | None = None) -> dict | None:
    cv = e.get("cv")
    if not cv:
        return None
    return {"status": cv.get("status"), "problems": cv.get("problems", []),
            "pdf": bool(cv.get("pdf")), "docx": bool(cv.get("docx")), "at": cv.get("at"),
            "fresh": master_sha is None or cv.get("master_sha") == master_sha}


def package_summary(pkg: dict | None) -> dict | None:
    """Light pointer for the list page: no questions, no refs, no personal
    data (state carries refs only, but rows stay light regardless)."""
    if not pkg:
        return None
    return {"status": pkg.get("status"), "channel": pkg.get("channel"),
            "auto_submit": pkg.get("auto_submit"), "prepared_at": pkg.get("prepared_at"),
            "approved": bool(pkg.get("approved"))}


def servable_path(recorded: str | None, jobs_dir: str) -> Path | None:
    """Mirror of contracts/rolegenie/domain.ts servablePath: only a path state
    records, inside the jobs folder, is ever served."""
    if not recorded:
        return None
    norm = lambda p: p.replace("\\", "/").rstrip("/").lower()  # noqa: E731
    r, root = norm(recorded), norm(jobs_dir)
    if ".." in r.split("/") or not r.startswith(root + "/"):
        return None
    return Path(recorded)


def reqs_sha(reqs: list) -> str:
    """A stable fingerprint of a requirements list, so the board and a decision can
    confirm they are talking about the same extraction (CR-rg-jd-decompose #2)."""
    return rolegenie.advert_sha(json.dumps(reqs, sort_keys=True, ensure_ascii=False))


def requirements_record(uid: str, entry: dict) -> list[dict]:
    """The posting's stored requirements, but only when the CURRENT advert, the state
    pointer and the record all agree: status 'ok' and one advert_sha shared by the
    stored JD, the pointer and the record. A failed/stale re-score or a changed JD
    therefore surfaces nothing, never an old file (review #2, #3). [] otherwise."""
    import tailor
    d = entry.get("decompose") or {}
    if d.get("status") != "ok":
        return []
    jd_file = tailor.jd_path(uid)
    if not jd_file.exists():
        return []
    current_sha = rolegenie.advert_sha(jd_file.read_text(encoding="utf-8"))
    if d.get("advert_sha") != current_sha:
        return []  # the advert changed since it was decomposed
    path = tailor.jd_decomp_path(uid)
    if not path.exists():
        return []
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    # Guard the shape (review #9): a non-object root, a non-list requirements value,
    # or non-dict members must degrade to [] rather than raise in the API.
    if not isinstance(rec, dict) or rec.get("advert_sha") != current_sha:
        return []
    reqs = rec.get("requirements")
    return [r for r in reqs if isinstance(r, dict)] if isinstance(reqs, list) else []


def requirements_view(uid: str, entry: dict, cfg: dict) -> tuple[list[dict], str]:
    """The ordered requirements for the review pane (owner/review priority attached)
    and their fingerprint, gated by requirements_record. The checklist shows at most
    CHECKLIST_LIMIT rows (owner cognitive-load bound, BRIEF-010) — a display bound only;
    the fingerprint covers every stored requirement."""
    reqs = requirements_record(uid, entry)
    out = []
    for r in rolegenie.order_by_priority(reqs, cfg)[: rolegenie.CHECKLIST_LIMIT]:
        op = rolegenie.owner_priority_of(r, cfg)
        out.append({**r, "ownerPriority": op,
                    "reviewPriority": rolegenie.review_priority(r.get("advertImportance", "contextual"), op)})
    return out, (reqs_sha(reqs) if reqs else "")


def job_detail(state: dict[str, dict], uid: str, cfg: dict | None = None) -> dict | None:
    """JD, decomposed requirements, tailored CV (without contact details) and
    alignment report for one posting."""
    import tailor
    e = state.get(uid)
    if e is None:
        return None
    cv = e.get("cv") or {}
    folder = Path(cv["folder"]) if cv.get("folder") else None
    jd_file = tailor.jd_path(uid)
    reqs_ordered, rsha = requirements_view(uid, e, cfg or {})
    detail = {"uid": uid, "jd": jd_file.read_text(encoding="utf-8") if jd_file.exists() else None,
              "cv": None, "report": None, "cv_summary": cv_summary(e),
              "requirements": reqs_ordered, "reqs_sha": rsha,
              "decompose": e.get("decompose") or {}, "package": e.get("package") or None}
    if detail["package"] and detail["package"].get("questions"):
        import apply
        answers = apply.answers_by_ref()
        refs = {q.get("ref") for q in detail["package"]["questions"] if q.get("ref")}
        detail["answers"] = {ref: {"label": answers[ref]["label"], "text": answers[ref]["text"]}
                             for ref in refs if ref in answers}
    if folder and (folder / "cv.json").exists():
        detail["cv"] = json.loads((folder / "cv.json").read_text(encoding="utf-8"))
        reports = sorted(folder.glob("*_Alignment_Report.md"))
        if reports:
            detail["report"] = reports[0].read_text(encoding="utf-8")
    # ATS parseability verdict, computed live from the drafted artifacts so it is
    # always current and needs no backfill (CR-rg-ats-check). It flags, never
    # gates: a blocked CV is still downloadable, just clearly marked.
    detail["ats"] = None
    if cv.get("status") == "drafted":
        try:
            import ats_check
            detail["ats"] = ats_check.check_cv(
                Path(cv["docx"]) if cv.get("docx") else None,
                Path(cv["pdf"]) if cv.get("pdf") else None,
                detail["cv"] or {})
        except Exception:  # noqa: BLE001 - a checker fault must not break the detail
            detail["ats"] = None
    return detail


def job_rows(state: dict[str, dict], master_sha: str | None = None) -> list[dict]:
    """What the page shows. Only fields from state: no personal data lives there."""
    rows = []
    for uid, e in state.items():
        decisions = e.get("decisions", [])
        rows.append({
            "cv": cv_summary(e, master_sha),
            "package": package_summary(e.get("package")),
            "excluded": e.get("excluded", ""),
            "expired": e.get("expired") or None,
            "application": (e.get("applications") or [None])[-1],
            "uid": uid,
            "board": e.get("board", ""),
            "title": (e.get("title") or "").strip(),
            "location": e.get("location", ""),
            "url": e.get("url", ""),
            "score": e.get("score"),
            "score100": e.get("score100"),
            "band": band(e.get("score")),
            # CR-rg-hydrate-gate: True = full advert and scored; False = excerpt held
            # pending hydration; None = legacy entry predating the flag.
            "hydrated": e.get("hydrated"),
            "verdict": e.get("verdict", ""),
            "first_seen": e.get("first_seen", ""),
            "decision": decisions[-1]["decision"] if decisions else None,
            "decisions": decisions,
            # CR-rg-contracts: which tab and section the posting belongs to.
            # Entries scored before categories existed get theirs from their facts.
            "category": e.get("category") or rolegenie.category_of(e.get("facts") or {}, False),
            "contract": {k: (e.get("facts") or {}).get(k) for k in ("ir35", "day_rate", "length")},
            "agencies": e.get("agencies", []),
            "source": e.get("source", ""),
            "ceiling": e.get("ceiling"),
            "ceiling_gaps": e.get("ceiling_gaps", []),
            # CR-rg-jd-decompose: light pointer only; the full list is in jd/<uid>.json.
            "decompose": e.get("decompose") or {},
        })
    # CR-rg-board-zero: review view ordered by the production fit score.
    def fit_key(r: dict) -> tuple:
        score = r["score100"]
        if not isinstance(score, (int, float)):
            score = r["score"] * 10 if isinstance(r["score"], int) else -1
        return -score, r["board"]

    rows.sort(key=fit_key)
    return rows


def driver_snapshots(uid: str, driver_ids: Any, entry: dict, submitted_sha: str | None) -> list[dict]:
    """Validate the ticked requirement ids against the posting's CURRENT, ok
    decomposition and snapshot them onto the decision (CR-rg-jd-decompose): id, text,
    kind, advertImportance and match as they stand now, so a later re-score cannot
    change what a past decision meant. Raises ValueError on a malformed array, a
    duplicate, an unknown id, a stale/failed record, or a fingerprint that no longer
    matches what the pane showed (#2, #3) — the pane is then told to refresh."""
    if not isinstance(driver_ids, list) or not all(isinstance(i, str) for i in driver_ids):
        raise ValueError("drivers must be a list of requirement ids")
    if len(driver_ids) != len(set(driver_ids)):
        raise ValueError("duplicate driver id")
    reqs = requirements_record(uid, entry)  # [] unless the pointer says current + ok
    if not reqs:
        raise ValueError("no current requirements for this posting; refresh the page")
    # The decision MUST carry the fingerprint of the extraction it was made against,
    # so an empty/missing hash can never submit a stale positional id (review #1).
    if not (isinstance(submitted_sha, str) and submitted_sha):
        raise ValueError("decision is missing the requirements fingerprint; refresh the page")
    if submitted_sha != reqs_sha(reqs):
        raise ValueError("the requirements changed since you opened this; refresh the page")
    reqmap = {r.get("id"): r for r in reqs}
    snaps = []
    for i in driver_ids:
        r = reqmap.get(i)
        if not r:
            raise ValueError(f"unknown requirement '{i}' (refresh the page)")
        snaps.append({k: r.get(k) for k in ("id", "text", "kind", "advertImportance", "match")})
    return snaps


def decide(state_path: Path, uid: str, decision: str, reason: str, drivers: Any = None,
           sha: str | None = None) -> dict:
    """Append one owner decision. Raises ValueError on a bad request. Drivers (the
    ticked requirements that drove the call) are kept for Apply and Reject only;
    Consider and Already-applied carry none."""
    reason = (reason or "").strip()
    if decision not in BOARD_DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(BOARD_DECISIONS)}")
    if len(reason) > 1000:
        raise ValueError("reason is too long")
    # Validate drivers whenever the field is present, not only when truthy (#8).
    if drivers is not None and not isinstance(drivers, list):
        raise ValueError("drivers must be a list of requirement ids")
    drivers = drivers or []
    snapshots: list[dict] = []
    if decision in ("apply", "reject") and drivers:
        entry = rolegenie.load_state(state_path).get(uid) or {}
        snapshots = driver_snapshots(uid, drivers, entry, sha)
    if decision == "reject" and not reason and not snapshots:
        raise ValueError("a reject needs a reason or a ticked driver")

    def record(st: dict[str, dict]) -> dict:
        if uid not in st:
            raise ValueError("unknown posting")
        entry = {
            "decision": decision,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "note": reason,
            "via": "board",
        }
        if snapshots:
            entry["drivers"] = snapshots
        st[uid].setdefault("decisions", []).append(entry)
        # CR-rg-outcome-loop: an Apply / Already-applied becomes an application record so the
        # funnel can follow it to an outcome. One per posting; referral defaults false (the
        # owner sets it later via `rolegenie.py mark ... --referral` or an outcome note).
        # BRIEF-038 (tester workflow = an approved master; the legacy record is unchanged): Apply is an INTENT (it asks
        # for a CV draft); "Already applied" is the person's own submission mark, recorded once with its time and the CV
        # reference in force. Only the mark counts as a submission.
        if decision in ("apply", "applied-externally"):
            import outcomes
            cv = st[uid].get("cv") or {}
            tester = rolegenie.approved_mode()
            if not st[uid].get("application"):
                st[uid]["application"] = outcomes.new_application(
                    channel="site" if decision == "apply" else "external",
                    cv_ref=cv.get("master_sha") or cv.get("folder"), referral=False,
                    now=entry["at"], intent=tester and decision == "apply")
            if tester and decision == "applied-externally":
                st[uid]["application"] = rolegenie.confirm_human_applied(st[uid], entry["at"])
        return st[uid]

    return rolegenie.update_state(state_path, record)


def make_handler(state_path: Path, allowed: list[str], jobs_dir: str = "", on_apply=None, master_sha=None, cfg: dict | None = None):
    """on_apply(uid) is called after an Apply tap is recorded: it starts the
    applier (D2). None (tests, or applying switched off) records the tap only."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "RoleGenieBoard/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _host_ok(self) -> bool:
            if allowed_host(self.headers.get("Host", ""), allowed):
                return True
            self._json(421, {"error": "unknown host"})
            return False

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            path, _, query = self.path.partition("?")
            q = {k: v[0] for k, v in parse_qs(query).items()}
            if path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/jobs":
                self._json(200, {"jobs": job_rows(rolegenie.load_state(state_path),
                                                  master_sha() if master_sha else None)})
            elif path == "/api/trust":
                import trust
                self._json(200, {"checkpoints": trust.summary(rolegenie.load_state(state_path),
                                                              trust.load_log())})
            elif path == "/api/brief":
                import brief
                latest = brief.LATEST
                self._json(200, json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {})
            elif path == "/api/job":
                detail = job_detail(rolegenie.load_state(state_path), q.get("uid", ""), cfg or {})
                self._json(200, detail) if detail else self._json(404, {"error": "unknown posting"})
            elif path == "/file":
                self._file(q.get("uid", ""), q.get("kind", ""))
            else:
                self._json(404, {"error": "not found"})

        def _file(self, uid: str, kind: str) -> None:
            if kind not in SERVABLE:
                self._json(400, {"error": "kind must be pdf or docx"})
                return
            cv = (rolegenie.load_state(state_path).get(uid) or {}).get("cv") or {}
            p = servable_path(cv.get(kind), jobs_dir)
            if p is None or not p.is_file():
                self._json(404, {"error": "no such file"})
                return
            self.send_response(200)
            body = p.read_bytes()
            self.send_header("Content-Type", SERVABLE[kind])
            self.send_header("Content-Length", str(len(body)))
            disposition = "inline" if kind == "pdf" else "attachment"
            self.send_header("Content-Disposition", f'{disposition}; filename="{p.name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            if self.path not in ("/api/decide", "/api/reinstate", "/api/approve"):
                self._json(404, {"error": "not found"})
                return
            if self.headers.get(WRITE_HEADER) != "1":
                self._json(403, {"error": f"missing {WRITE_HEADER} header"})
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": "send JSON"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._json(413, {"error": "bad body size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/reinstate":
                    import trust
                    self._json(200, {"ok": True, "entry": trust.reinstate(str(body.get("checkpoint", "")))})
                    return
                if self.path == "/api/approve":
                    uid = str(body.get("uid", ""))
                    if rolegenie.approved_mode():     # BRIEF-038: record nothing, start nothing
                        raise ValueError("automatic submission is off in this workflow: submit it yourself, "
                                         "then mark it applied")

                    def approve(st: dict[str, dict]) -> None:
                        if uid not in st:
                            raise ValueError("unknown posting")
                        decisions = st[uid].get("decisions") or []
                        # Mirror the board's LEGACY mapping; only the latest tap counts.
                        if not decisions or decisions[-1].get("decision") not in ("apply", "applied"):
                            raise ValueError("the latest decision is not Apply")
                        pkg = st[uid].get("package") or {}
                        if pkg.get("status") != "ready":
                            raise ValueError("no ready application package; prepare it before approving")
                        if not pkg.get("auto_submit"):
                            raise ValueError("manual-submit channel - submit on the employer's site, then Mark applied-externally")
                        if master_sha and pkg.get("master_sha") != master_sha():
                            raise ValueError("the master CV has changed; prepare a fresh package before approving")
                        if pkg.get("approved"):
                            raise ValueError("already approved")
                        pkg["approved"] = {"at": dt.datetime.now().isoformat(timespec="seconds")}

                    rolegenie.update_state(state_path, approve)
                    started = "waiting-switch"
                    if (cfg or {}).get("apply", {}).get("enabled"):
                        import apply
                        apply.start_in_background(uid, "apply")
                        started = "apply"
                    self._json(200, {"ok": True, "started": started})
                    return
                entry = decide(state_path, str(body.get("uid", "")),
                               str(body.get("decision", "")), str(body.get("reason", "")),
                               body.get("drivers"), body.get("reqs_sha"))
            except (ValueError, json.JSONDecodeError, AttributeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except TimeoutError as exc:
                self._json(503, {"error": str(exc)})
                return
            started = "none"
            if on_apply and body.get("decision") == "apply":
                started = on_apply(str(body.get("uid")), entry)
            self._json(200, {"ok": True, "decisions": entry["decisions"], "started": started})

    return Handler


def serve(cfg: dict) -> int:
    bc = cfg.get("board", {})
    port = int(bc.get("port", 8765))
    state_path = ROOT / cfg["output"]["state_file"]
    handler = make_handler(state_path, list(bc.get("allowed_hosts", ["127.0.0.1", "localhost"])),
                           str(cfg.get("tailor", {}).get("jobs_dir", "")),
                           _applier(cfg), _master_sha(cfg), cfg)
    threads = [threading.Thread(target=_listen, args=(host, port, handler), daemon=True)
               for host in bc.get("listen", ["127.0.0.1"])]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def _applier(cfg: dict):
    """The Apply-tap hook: re-draft a CV from an older master, else run the
    applier if applying is switched on."""
    import apply
    import tailor
    enabled = bool((cfg.get("apply") or {}).get("enabled"))
    return lambda uid, entry: apply.on_apply_tap(entry, uid, apply.workflow_master_sha(cfg) or "", enabled)


def _master_sha(cfg: dict):
    import tailor
    import apply
    return lambda: apply.workflow_master_sha(cfg) or ""


def _listen(host: str, port: int, handler, retry_seconds: int = 60) -> None:
    """Serve on one address. An address that isn't up yet (Tailscale starting
    after logon) is retried every minute rather than dropped."""
    while True:
        try:
            server = ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            print(f"Not listening on {host} yet: {exc}", file=sys.stderr)
            time.sleep(retry_seconds)
            continue
        print(f"Board on http://{host}:{port}/", file=sys.stderr)
        server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(serve(rolegenie.load_config()))  # the data-dir config and ledger (CR-rg-user-data-home)
