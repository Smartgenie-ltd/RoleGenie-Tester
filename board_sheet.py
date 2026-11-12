"""Phone board (BRIEF-025, CR-rg-board-sheet; contract contracts/rolegenie/board-sheet.ts).

The board is a Google Apps Script web app in the user's own Google account (apps-script/), over a Google Sheet in their
Drive. This module talks to it with plain `requests` (no Google client library, no OAuth):

  rolegenie.py board setup        create the board secret (~/.rolegenie/board.json) and print the deploy steps
  rolegenie.py board setup URL    store the web app URL and print the phone link
  rolegenie.py board push         send every assessment in ~/.rolegenie/assessments/ to the board
  rolegenie.py board pull         write the decisions tapped on the board to ~/.rolegenie/decisions.json (the latest per
                                  job), merge the board's decision history into ~/.rolegenie/decision-events.json and
                                  import each tap exactly once into the scan state (state.json decisions, BRIEF-048)

Every request carries the secret in its body (never in an API URL). The secret is shown in full only by `setup`.
Apply never applies (D5a): a tap is recorded, nothing is submitted. BRIEF-048 (CR-rg-daily-phone-board) connects the
board to the daily workflow: push records which posting each board row is (board-identity.json, from the assessments,
never from titles) and shows a draft/held status and a user-supplied https document link (board-docs.json); pull
appends each ledger event to state.json under the state lock, once by event id. A tap whose job cannot be tied to
exactly one posting is held, listed in decision-held.json and imported once a later push resolves it.
"""
import datetime as dt
import json
import re
import secrets
import sys
from pathlib import Path

import requests

import onboard

DECISIONS = ("apply", "consider", "reject", "applied-externally")   # board-sheet.ts SHEET_DECISIONS
REASON_REQUIRED = ("apply", "reject")
EVENT_COLUMNS = ("eventId", "requestId", "uid", "decision", "reason", "decidedAt", "source")  # board-sheet.ts
EVENT_SOURCES = ("tap", "legacy-snapshot")
HISTORY_VERSION = 1
DOC_COLUMNS = ("cvStatus", "docLink", "docNote")     # board-sheet.ts DOC_COLUMNS
CV_STATUSES = ("", "draft", "held")
LISTS = ("unmetHard", "confirmedGaps", "missingEvidence", "uncertain")
TIMEOUT = 60
USAGE = "usage: rolegenie.py board setup [WEB_APP_URL] | board push | board pull   (README: Phone board)"


class BoardError(Exception):
    """The board could not be reached, refused the request, or is not set up. The message never holds the secret."""


def decision_problems(d: dict) -> list[str]:
    """Mirror of decisionProblems in board-sheet.ts (tied by tests/fixtures/board/contract-cases.json)."""
    problems = []
    if str(d.get("uid") or "").strip() == "":
        problems.append("decision needs a job uid")
    if d.get("decision") not in DECISIONS:
        problems.append(f"unknown decision '{d.get('decision')}'")
    elif d["decision"] in REASON_REQUIRED and str(d.get("reason") or "").strip() == "":
        problems.append(f"{d['decision']} needs a reason")
    return problems


def decision_event_problems(e: dict) -> list[str]:
    """Mirror of decisionEventProblems in board-sheet.ts (same shared cases)."""
    def text(k):
        return e.get(k) if isinstance(e.get(k), str) else ""
    problems = decision_problems({"uid": text("uid"), "decision": text("decision"), "reason": text("reason")})
    if text("eventId").strip() == "":
        problems.append("event needs an eventId")
    if text("decidedAt").strip() == "":
        problems.append("event needs a decidedAt")
    if text("source") not in EVENT_SOURCES:
        problems.append(f"unknown event source '{text('source')}'")
    return problems


def masked(secret: str) -> str:
    return secret[:4] + "…"


def _config(need_url: bool = True) -> dict:
    cfg = onboard.load("board.json", {})
    if not cfg.get("secret") or (need_url and not cfg.get("url")):
        raise BoardError("the board is not set up: run `rolegenie.py board setup` (README: Phone board)")
    return cfg


def _post(cfg: dict, body: dict) -> dict:
    try:
        r = requests.post(cfg["url"], data=json.dumps({**body, "secret": cfg["secret"]}),
                          headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise BoardError(f"cannot reach the board: {type(exc).__name__}") from None
    try:
        res = r.json()
    except ValueError:
        raise BoardError("the board did not answer with data: check it is deployed as a web app with "
                         "'Execute as: Me' and 'Who has access: Anyone' (README: Phone board)") from None
    if not isinstance(res, dict) or res.get("ok") is not True:
        err = res.get("error") if isinstance(res, dict) else None
        if err == "refused":
            raise BoardError("the board refused the request: the secret in Script Properties (BOARD_SECRET) "
                             "does not match ~/.rolegenie/board.json")
        raise BoardError(f"the board said: {err or 'no answer'}")
    return res


def _lines(a: dict, key: str) -> str:
    by = {r["id"]: r for r in a.get("requirements", [])}
    return "\n".join(f"{i} {by[i]['text']}" if i in by else i for i in a.get(key, []))


def _state_path(cfg: dict):
    import rolegenie
    return rolegenie.ROOT / cfg["output"]["state_file"]


def _load_state(cfg: dict | None) -> dict:
    """The scan state if there is one; never creates it."""
    import rolegenie
    path = _state_path(cfg) if cfg else None
    return rolegenie.load_state(path) if path and path.exists() else {}


def doc_problems(d: dict) -> list[str]:
    """Mirror of docProblems in board-sheet.ts (same shared cases); `d` is the MERGED document block."""
    status, link, note = (str(d.get(k) or "").strip() for k in DOC_COLUMNS)
    problems = []
    if status not in CV_STATUSES:
        problems.append(f"unknown cvStatus '{status}'")
    if link and not re.fullmatch(r"https://\S+", link, re.I):
        problems.append("docLink must be an https link")
    if link and status == "":
        problems.append("docLink needs a cvStatus")
    if status == "held" and note == "":
        problems.append("held needs a docNote")
    return problems


def resolve_identity(links: dict | None, board_uid: str, known: list) -> dict:
    """Mirror of resolveBoardIdentity in board-sheet.ts: one posting for one board row, both ways, else held."""
    if not links:
        return {"kind": "held", "reason": "unknown"}
    if board_uid in links.get("ambiguous", {}):
        return {"kind": "held", "reason": "ambiguous"}
    if board_uid not in links.get("links", {}):
        return {"kind": "held", "reason": "unknown"}
    posting = links["links"][board_uid]
    if sum(1 for p in links["links"].values() if p == posting) > 1:
        return {"kind": "held", "reason": "ambiguous"}
    if posting not in known:
        return {"kind": "held", "reason": "unknown"}
    return {"kind": "resolved", "postingUid": posting}


def state_decision_for(decision: str) -> str:
    """Mirror of stateDecisionFor: Already applied is the recorded mark `applied`; the rest keep their name."""
    return "applied" if decision == "applied-externally" else decision


def _latest_assessments() -> dict:
    """The latest assessment per board uid (assessment.ts boardUid), stale or not. A link assessed again updates its
    one row: the latest wins."""
    from assess import board_uid
    found = []
    for f in (onboard.data_dir() / "assessments").glob("assessment-*.json"):
        a = json.loads(f.read_text(encoding="utf-8"))
        found.append((str(a.get("assessedAt") or ""), f.name, a))
    return {board_uid(a): a for _, _, a in sorted(found, key=lambda t: t[:2])}   # oldest first, later replaces


def _is_stale(a: dict) -> bool:
    """Assessed against a master that is no longer the approved one (047's open boundary): not pushed."""
    master = onboard.load("master.json", None)
    return bool(master and a.get("masterSha") and a["masterSha"] != onboard.master_sha(master))


def build_links(latest: dict, state: dict) -> dict:
    """BoardLinks (board-sheet.ts): board uid -> posting uid, from the assessments, never from a title. The file's
    postingUid wins; else the one state entry whose normalised link equals the assessment's. Several: ambiguous."""
    from assess import normalise_ad_url
    by_url: dict = {}
    for uid, e in state.items():
        if e.get("url"):
            by_url.setdefault(normalise_ad_url(e["url"]), []).append(uid)
    links, ambiguous = {}, {}
    for buid, a in latest.items():
        if a.get("postingUid"):
            links[buid] = a["postingUid"]
        elif a.get("url"):
            hits = by_url.get(normalise_ad_url(a["url"]), [])
            if len(hits) == 1:
                links[buid] = hits[0]
            elif len(hits) > 1:
                ambiguous[buid] = sorted(hits)
    return {"version": 1, "links": links, "ambiguous": ambiguous}


def _doc_fields(buid: str, docs: dict) -> dict:
    """The document keys to push for a row: only those the user's tools supplied; a bad entry goes up as `held` with the
    reason (never silently dropped, never a non-https link)."""
    d = docs.get(buid)
    if not isinstance(d, dict):
        return {}
    sup = {k: str(d[k] or "").strip() for k in DOC_COLUMNS if k in d}
    problems = doc_problems({k: sup.get(k, "") for k in DOC_COLUMNS}) if sup else []
    if problems:
        return {"cvStatus": "held", "docLink": "", "docNote": "supplied document refused: " + "; ".join(problems)}
    return sup


def rows_from_assessments(state: dict | None = None) -> list[dict]:
    """One AssessmentRow per board uid (assessment.ts boardUid): company and link as recorded at `assess`, blank when
    absent (assessments written before BRIEF-029). The title and company of the posting win when the assessment names its
    posting and the scan state has it (BRIEF-048). Stale assessments are not pushed (see _is_stale)."""
    rows, _ = _rows_and_stale(state or {})
    return rows


def _rows_and_stale(state: dict) -> tuple[list[dict], int]:
    docs = onboard.load("board-docs.json", {})
    rows, stale = [], 0
    for buid, a in _latest_assessments().items():
        if _is_stale(a):
            stale += 1
            continue
        ad, url = str(a.get("ad") or ""), str(a.get("url") or "")
        title = ("(ad from link)" if url and ad == url.strip() else
                 Path(ad).stem if ad and ad != "stdin" else "(pasted ad)")
        posting = state.get(a.get("postingUid") or "") or {}
        rows.append({"uid": buid, "title": str(posting.get("title") or title), "company": str(a.get("company") or ""),
                     "url": url, "score100": a["score100"], **{k: _lines(a, k) for k in LISTS},
                     "assessedAt": a.get("assessedAt", ""), **_doc_fields(buid, docs)})
    return rows, stale


STEPS = """Deploy your phone board (once, in your own Google account; the same on Windows and Linux):
  1. Create a new Google Sheet (sheets.new). Then Extensions > Apps Script.
  2. Replace the code in Code.gs with apps-script/Code.gs from this folder. Add an HTML file named Page
     (File > + > HTML) and paste apps-script/Page.html into it. Save.
  3. Project Settings (gear) > Script Properties > Add: property BOARD_SECRET, value:
       {secret}
  4. Deploy > New deployment > type Web app. Execute as: Me. Who has access: Anyone. Deploy, and allow access.
  5. Copy the Web app URL (ends in /exec) and run:  python rolegenie.py board setup WEB_APP_URL"""


def cmd_setup(argv: list[str]) -> int:
    cfg = onboard.load("board.json", {})
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_urlsafe(32)
        _save(cfg)
        print(STEPS.format(secret=cfg["secret"]))
        print("\nThe secret is stored in", onboard.data_dir() / "board.json", "and is not shown again.")
    elif not argv:
        print(STEPS.format(secret=f"(already created: {masked(cfg['secret'])}  - the full value is in "
                                  f"{onboard.data_dir() / 'board.json'})"))
    if argv:
        url = argv[0].strip()
        if not url.startswith("https://"):
            print("The web app URL must start with https:// (Deploy > Manage deployments > Web app URL).",
                  file=sys.stderr)
            return 1
        cfg["url"] = url
        _save(cfg)
        print("Board URL saved. Open this link on your phone and bookmark it (it carries your secret; keep it "
              "private):\n  " + f"{url}?s={cfg['secret']}")
        print("Next: python rolegenie.py board push")
    return 0


def _save(cfg: dict) -> None:
    onboard.save("board.json", cfg)   # written via mkstemp: owner-only (0600) on Linux, user-profile ACL on Windows


def cmd_push(cfg: dict | None = None) -> int:
    board_cfg = _config()
    state = _load_state(cfg)
    rows, stale = _rows_and_stale(state)
    if not rows:
        if stale:
            print(f"{stale} assessment(s) were made against an older master and are not pushed: assess the jobs "
                  "again (`rolegenie.py assess --pending`) on your approved master.", file=sys.stderr)
        else:
            print("No assessments yet: run `rolegenie.py assess AD_FILE` first.", file=sys.stderr)
        return 1
    res = _post(board_cfg, {"action": "push", "rows": rows})
    links = build_links(_latest_assessments(), state)
    onboard.save("board-identity.json", links)       # only after the board took the push
    print(f"Board updated: {res.get('added', 0)} added, {res.get('updated', 0)} updated "
          "(decisions already on the board are kept).")
    if stale:
        print(f"{stale} assessment(s) made against an older master were not pushed.")
    print(f"Identity: {len(links['links'])} job(s) tied to a posting, {len(links['ambiguous'])} ambiguous.")
    if any(k in r for r in rows for k in DOC_COLUMNS) and not res.get("docsVersion"):
        print("Your board runs an older version of apps-script/Code.gs and cannot show documents: paste the current "
              "Code.gs and Page.html into the Apps Script editor, then Deploy > Manage deployments > Edit > New "
              "version.", file=sys.stderr)
    return 0


UPGRADE = ("your board runs an older version of apps-script/Code.gs and cannot send decision history, so only the "
           "latest decision per job was pulled. Paste the current apps-script/Code.gs and Page.html into the Apps "
           "Script editor, then Deploy > Manage deployments > Edit > New version (this keeps the same URL)")


def _decisions_from(res: dict) -> list[dict]:
    out = []
    for d in res["decisions"]:
        d = {"uid": str(d.get("uid", "")), "decision": d.get("decision"), "reason": str(d.get("reason") or ""),
             "at": str(d.get("decidedAt") or "")}
        problems = decision_problems(d)
        if problems:
            print(f"Skipped {d['uid']}: {'; '.join(problems)} (edit it on the board)", file=sys.stderr)
            continue
        out.append(d)
    return out


def _merge_events(local: list, pulled: list) -> list:
    """Idempotent by eventId: the local order is kept, new events follow in the board's order. An event whose id is
    known but whose content differs keeps the local copy and warns; an invalid event is skipped with a warning."""
    merged = {e["eventId"]: e for e in local if isinstance(e, dict) and e.get("eventId")}
    for raw in pulled:
        e = {k: raw.get(k, "") for k in EVENT_COLUMNS}
        e = {k: v if isinstance(v, str) else "" for k, v in e.items()}
        problems = decision_event_problems(e)
        if problems:
            print(f"Skipped history event {e['eventId'] or '(no id)'}: {'; '.join(problems)}", file=sys.stderr)
        elif e["eventId"] not in merged:
            merged[e["eventId"]] = e
        elif merged[e["eventId"]] != e:
            print(f"History event {e['eventId']} differs from the copy already kept here; the kept copy stays.",
                  file=sys.stderr)
    return list(merged.values())


HELD_TEXT = {"unknown": "no posting is recorded for this job on this machine (run `board push` to resolve)",
             "ambiguous": "more than one posting could be this job, so none was chosen"}


def _local_iso(stamp: str) -> str:
    """The board's UTC time as local time to the second, as `mark` records it. ValueError when unreadable."""
    return (dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            .astimezone().replace(tzinfo=None).isoformat(timespec="seconds"))


def import_decision_events(state: dict, events: list, links: dict | None) -> dict:
    """PullImport (board-sheet.ts): append every ledger event not yet in `state` to its posting's decisions (the order
    kept, same-second taps stable), once by event id. Driven by "event id absent from state", not "new in the ledger",
    so a pull that died after the ledger was saved is completed by the next one. Mutates `state`; call it inside
    rolegenie.update_state so the lock and re-read protect concurrent scan and `mark` writes."""
    known = {d["eventId"] for e in state.values() for d in e.get("decisions", []) if d.get("eventId")}
    imported, held, already, touched = [], [], 0, set()
    for ev in events:
        if ev["eventId"] in known:
            already += 1
            continue
        ident = resolve_identity(links, ev["uid"], list(state))
        try:
            at = _local_iso(ev["decidedAt"])
        except ValueError:
            held.append({"eventId": ev["eventId"], "boardUid": ev["uid"], "reason": "its time could not be read"})
            continue
        if ident["kind"] == "held":
            held.append({"eventId": ev["eventId"], "boardUid": ev["uid"], "reason": HELD_TEXT[ident["reason"]]})
            continue
        sd = state_decision_for(ev["decision"])
        state[ident["postingUid"]].setdefault("decisions", []).append(
            {"decision": sd, "at": at, "note": ev["reason"], "eventId": ev["eventId"], "boardUid": ev["uid"],
             "boardDecision": ev["decision"], "source": ev["source"]})
        touched.add(ident["postingUid"])
        known.add(ev["eventId"])
        imported.append({"eventId": ev["eventId"], "boardUid": ev["uid"], "postingUid": ident["postingUid"],
                         "decision": sd, "boardDecision": ev["decision"], "reason": ev["reason"],
                         "decidedAt": ev["decidedAt"], "source": ev["source"]})
    for uid in touched:
        state[uid]["decisions"].sort(key=lambda d: d["at"])      # stable: same-second taps keep ledger order
    return {"imported": imported, "held": held, "alreadyKnown": already}


def pull_decisions(cfg: dict) -> dict:
    """Pull the board, keep its ledger, import it into the scan state; returns PullImport for the integrator (038)."""
    board_cfg = _config()
    res = _post(board_cfg, {"action": "pull"})
    bad = "local history is unchanged"
    if not isinstance(res.get("decisions"), list) or not all(isinstance(d, dict) for d in res["decisions"]):
        raise BoardError(f"the board's answer has no usable decisions list; {bad}")
    old_server = "historyVersion" not in res and "events" not in res
    if not old_server:
        if res.get("historyVersion") != HISTORY_VERSION:
            raise BoardError(f"the board sent history version {res.get('historyVersion')!r}, this RoleGenie reads "
                             f"{HISTORY_VERSION}: update RoleGenie or the board; {bad}")
        if not isinstance(res.get("events"), list) or not all(isinstance(e, dict) for e in res["events"]):
            raise BoardError(f"the board's history is malformed; {bad}")
    decisions = _decisions_from(res)
    if old_server:
        path = onboard.save("decisions.json", decisions)
        print(f"Pulled {len(decisions)} decision(s) into {path}")
        raise BoardError(UPGRADE)
    events = _merge_events(onboard.load("decision-events.json", []), res["events"])
    path = onboard.save("decisions.json", decisions)
    epath = onboard.save("decision-events.json", events)      # durable first; the import below can be repeated
    print(f"Pulled {len(decisions)} decision(s) into {path} and {len(events)} event(s) into {epath}")
    if any(e["source"] == "legacy-snapshot" for e in events):
        print("Some events are legacy snapshots: they record a decision as it stood when history began. "
              "Earlier history cannot be reconstructed.")
    links = onboard.load("board-identity.json", None)
    import rolegenie
    state_path = _state_path(cfg)
    if state_path.exists():
        try:
            result = rolegenie.update_state(state_path, lambda st: import_decision_events(st, events, links))
        except TimeoutError:
            raise BoardError("the scan state is locked by another writer; your decisions are kept in "
                             "decision-events.json and import on the next pull") from None
    else:
        result = import_decision_events({}, events, links)        # nothing to import into; never creates the state
        for h in result["held"]:
            h["reason"] = "no scan state yet: run a scan first (" + h["reason"] + ")"
    onboard.save("decision-held.json", result["held"])
    return result


def cmd_pull(cfg: dict | None = None) -> int:
    if cfg is None:
        raise BoardError("no configuration: run it as `rolegenie.py board pull`")
    result = pull_decisions(cfg)
    legacy = sum(1 for i in result["imported"] if i["source"] == "legacy-snapshot")
    print(f"Imported {len(result['imported'])} decision(s) into your workflow state "
          f"({result['alreadyKnown']} already there)."
          + (f" {legacy} of them are legacy snapshots (the original tap time is unknown)." if legacy else ""))
    if result["held"]:
        print(f"Held {len(result['held'])} decision(s): kept, not lost (decision-held.json); each imports once its job "
              "can be tied to one posting.")
        for h in result["held"]:
            print(f"  {h['eventId']}  {h['boardUid']}: {h['reason']}")
    return 0


def cmd_board(argv: list[str], cfg: dict | None = None) -> int:
    onboard._out()
    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    if sub not in ("setup", "push", "pull") or (sub != "setup" and rest) or len(rest) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if sub == "setup":
            return cmd_setup(rest)
        return cmd_push(cfg) if sub == "push" else cmd_pull(cfg)
    except BoardError as exc:
        print(f"Board: {exc}", file=sys.stderr)
        return 1
