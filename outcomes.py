"""BRIEF-014 outcome loop (CR-rg-outcome-loop): application records, the outcome state
machine, the lapse pass, the inbox matcher, and funnel maths. Pure/stdlib - no mail-reading
code and no mail credentials live here (Gmail is a session-connector ritual). Jev is the ONLY
network call (email classification), single-sourced from the skill module and only reached
through an injected classifier so the deterministic matcher and all tests run with no key.

Python mirror of contracts/rolegenie/domain.ts (OUTCOME_STATES / OUTCOME_TRANSITIONS /
APPLY_CHANNELS / LAPSE_DAYS / outcomeCanTransition + ApplicationRecord / OutcomeEvent shapes).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

OUTCOME_STATES = ("open", "rejected", "interview", "offer", "landed", "lapsed")
APPLY_CHANNELS = ("site", "manual", "external", "referral", "other")
OUTCOME_TRANSITIONS = {
    "open": ("rejected", "interview", "lapsed"),
    "interview": ("offer", "rejected", "lapsed"),
    "offer": ("landed", "rejected"),
    "lapsed": ("rejected", "interview"),
    "rejected": (),
    "landed": (),
}
LAPSE_DAYS = 28
TERMINAL = ("rejected", "landed")


def outcome_can_transition(frm: str, to: str) -> bool:
    return to in OUTCOME_TRANSITIONS.get(frm, ())


# --- application records -----------------------------------------------------

def new_application(channel: str, cv_ref: str | None, referral: bool, now: str | None = None,
                    intent: bool = False) -> dict:
    """A fresh application record in `open` state (mirror of domain.ts ApplicationRecord).
    `intent=True` marks an Apply decision the person has not yet confirmed as submitted (BRIEF-051)."""
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    if channel not in APPLY_CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(APPLY_CHANNELS)}")
    app = {"appliedAt": now, "channel": channel, "cvRef": cv_ref, "referral": bool(referral),
           "outcome": "open", "outcomeAt": now, "note": "",
           "history": [{"state": "open", "at": now, "note": "", "evidence": "apply"}]}
    if intent:
        app["submission"] = "intent"
    return app


def record_submission(app: dict | None, channel: str, cv_ref: str | None,
                      now: str | None = None) -> dict:
    """CR-rg-apply-stage1: record a machine submission, preserving owner history."""
    recorded_channel = channel if channel in APPLY_CHANNELS else (
        "manual" if channel in ("workday", "manual") else "site")
    if app is None:
        app = new_application(recorded_channel, cv_ref, False, now)
    else:
        app["channel"] = recorded_channel
        app["cvRef"] = cv_ref
    app["ats"] = channel
    return app


def transition(app: dict, to: str, note: str = "", evidence: str = "owner",
               now: str | None = None) -> dict:
    """Return a copy of `app` moved to state `to`, history appended. Raises ValueError on an
    illegal transition (validated against OUTCOME_TRANSITIONS). Append-only: the prior states
    and their timestamps are kept (a late rejection after lapse keeps both)."""
    frm = app.get("outcome", "open")
    if to not in OUTCOME_STATES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOME_STATES)}")
    if not outcome_can_transition(frm, to):
        legal = ", ".join(OUTCOME_TRANSITIONS.get(frm, ())) or "(none - terminal)"
        raise ValueError(f"illegal outcome transition {frm} -> {to} (legal: {legal})")
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    out = dict(app)
    out["outcome"] = to
    out["outcomeAt"] = now
    out["note"] = (note or "").strip()[:500]
    out["history"] = list(app.get("history", [])) + [
        {"state": to, "at": now, "note": out["note"], "evidence": (evidence or "owner")[:200]}]
    return out


def due_for_lapse(app: dict, lapse_days: int, now: dt.datetime | None = None) -> bool:
    """True when an `open` application has had no feedback for longer than the window."""
    if app.get("outcome") != "open":
        return False
    now = now or dt.datetime.now()
    try:
        applied = dt.datetime.fromisoformat(app.get("appliedAt", ""))
    except ValueError:
        return False
    return (now - applied) >= dt.timedelta(days=lapse_days)


# --- inbox matcher (layer 1: deterministic; layer 2: Jev; layer 3: owner confirms) ----------

_STOP = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "at", "senior", "lead",
         "staff", "principal", "engineer", "developer", "manager", "specialist", "role", "ltd",
         "limited", "inc", "llc", "gmbh", "plc", "team", "uk", "remote"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def _domain(sender: str) -> str:
    m = re.search(r"@([a-z0-9.\-]+)", (sender or "").lower())
    host = m.group(1) if m else (sender or "").lower()
    # strip common mail subdomains so "careers.acme.com" and "acme.com" both read as "acme"
    parts = [p for p in host.split(".") if p not in
             ("com", "co", "uk", "org", "net", "io", "ai", "www", "mail", "careers",
              "jobs", "noreply", "no-reply", "talent", "recruiting", "hr", "notifications")]
    return parts[-1] if parts else host


def _deterministic_signals(sender, subject, body, email_date, company, title, applied_at):
    """What fired, for one candidate application. Date must be on/after the apply."""
    after = True
    try:
        if email_date and applied_at:
            after = dt.datetime.fromisoformat(str(email_date)[:19]) >= dt.datetime.fromisoformat(str(applied_at)[:19])
    except ValueError:
        after = True
    comp = _tokens(company)
    dom = _domain(sender)
    domain_match = bool(comp) and any(c in dom or dom in c for c in comp)
    title_toks = _tokens(title)
    text_toks = _tokens(f"{subject} {body}")
    overlap = len(title_toks & text_toks) / len(title_toks) if title_toks else 0.0
    score = (2.0 if domain_match else 0.0) + 1.5 * overlap + (0.0 if after else -5.0)
    return {"domain_match": domain_match, "sender_domain": dom, "title_overlap": round(overlap, 3),
            "after_apply": after}, round(score, 3)


def match_email(sender: str, subject: str, body: str, email_date: str,
                applications: list[dict], classify=None) -> dict:
    """Propose which open application(s) an email concerns. `applications` is a list of
    {uid, company, title, appliedAt}. Returns {label, candidates:[{uid, score, signals}...]}
    ranked best-first; only candidates on/after their apply with a real signal are kept. Layer
    2 (Jev) classifies the email ONCE via the injected `classify`; it is evidence, not a write.
    This function NEVER mutates state - the owner confirms a proposal downstream."""
    cands = []
    for a in applications:
        sig, score = _deterministic_signals(sender, subject, body, email_date,
                                            a.get("company", ""), a.get("title", ""), a.get("appliedAt", ""))
        if sig["after_apply"] and (sig["domain_match"] or sig["title_overlap"] > 0.0):
            cands.append({"uid": a["uid"], "score": score, "signals": sig})
    cands.sort(key=lambda c: (-c["score"], c["uid"]))
    label = classify(sender, subject, body) if classify else None
    return {"label": label, "candidates": cands}


# Jev email classifier (layer 2). Single-sourced client pattern from the skill module; email
# text is UNTRUSTED (sanitized, instruction-shaped content stripped). Choice with an `other`
# escape. Returns {label, confidence, probabilities} or raises JevUnavailable (no fallback).
EMAIL_OPTS = ("rejection", "interview-invite", "acknowledgement", "other")
EMAIL_CRITERIA = {
    "rejection": {"what": "the email declines the application - not moving forward, position filled, pursuing other candidates.",
                  "not_for": "an invitation to interview or a next step (interview-invite); a mere receipt/under-review note (acknowledgement); anything else (other)."},
    "interview-invite": {"what": "the email invites the candidate to interview, a call, an assessment, or a concrete next step, or asks for availability to meet.",
                         "not_for": "a decline (rejection); a bare we-received-your-application note (acknowledgement); anything else (other)."},
    "acknowledgement": {"what": "the email only confirms receipt or that the application is under review, with no decision and no next step.",
                        "not_for": "a decline (rejection); an interview/next-step invite (interview-invite); anything unrelated (other)."},
    "other": {"what": "the email does not fit the above - a job alert, newsletter, unrelated mail, or too little to tell.",
              "not_for": "a clear decline, interview invite, or receipt acknowledgement."},
}


def _sanitize_email(sender: str, subject: str, body: str) -> dict:
    """Strip instruction-shaped text from untrusted email and bound length."""
    def clean(t: str, n: int) -> str:
        t = " ".join((t or "").split())
        low = t.lower()
        for m in ("ignore previous", "ignore the above", "disregard", "system prompt",
                  "you are now", "new instructions"):
            if m in low:
                return "[omitted: instruction-shaped]"
        return t[:n]
    return {"sender": clean(sender, 200), "subject": clean(subject, 300), "body": clean(body, 2000)}


def jev_classify_email(sender: str, subject: str, body: str, client=None):
    """Classify one email with Jev (pinned jev-1.13.0). Raises JevUnavailable with no fallback.
    Imports the production scorer's single-sourced helpers so the skill conformance is shared."""
    import jev_scoring  # MODEL, jj (load_key, est_tokens), JevUnavailable
    import typesafe_sdk as ts
    jj = jev_scoring.jj
    if not jj.load_key():
        raise jev_scoring.JevUnavailable("TYPESAFE_API_KEY absent")
    state = {"email": _sanitize_email(sender, subject, body)}
    q = {"email_kind": ts.Choice(
            instructions={"question": "Classify `email` (an email the candidate received after "
                                      "applying for a job) as one of the options."},
            criteria=EMAIL_CRITERIA)}
    assert jj.est_tokens(state) + jj.est_tokens({k: v.model_dump() for k, v in q.items()}) < 60000
    own = client is None
    if own:
        client = ts.TypeSafeClient(model=jev_scoring.MODEL)
    try:
        resp = client.system_one(state=state, questions=q, model=jev_scoring.MODEL)
    except Exception as exc:  # noqa: BLE001
        raise jev_scoring.JevUnavailable(str(exc)[:200]) from exc
    finally:
        if own:
            client.close()
    if resp.model != jev_scoring.MODEL:
        raise jev_scoring.JevUnavailable(f"model drift: {resp.model}")
    a = resp.answers["email_kind"]
    return {"label": a.choice, "confidence": round(a.confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in a.probabilities.items()}}


# --- funnel maths (pure; deterministic over a state dict) -------------------

def funnel_counts(state: dict, above_bar) -> dict:
    """The owner's funnel as numbers from a state dict. `above_bar(entry)->bool` decides the
    above-bar step (score at/above the review threshold and not hard-excluded). Deterministic."""
    surfaced = above = applied = 0
    sub = {"submitted": 0, "intent": 0, "legacy-ambiguous": 0}
    out = {s: 0 for s in OUTCOME_STATES}
    for e in state.values():
        if not isinstance(e, dict):
            continue
        a = e.get("assessment")   # BRIEF-064: an approved-master entry keeps its score in this pointer
        if isinstance(e.get("score"), int):
            scored = True
        elif isinstance(a, dict):
            scored = a.get("status") == "assessed" and isinstance(a.get("score100"), (int, float))
        else:
            continue                # legacy entry with no score: not in the funnel (unchanged)
        if scored:
            surfaced += 1
            if above_bar(e):
                above += 1
        app = e.get("application")
        if isinstance(app, dict):
            sub[submission_status(app)] += 1
            if submission_status(app) == "intent":      # an Apply tap is not an application until the person marks it
                continue
            applied += 1
            out[app.get("outcome", "open")] = out.get(app.get("outcome", "open"), 0) + 1
    interviews = out["interview"] + out["offer"] + out["landed"]  # reached interview or beyond
    return {"surfaced": surfaced, "above_bar": above, "applied": applied,
            "open": out["open"], "rejected": out["rejected"], "lapsed": out["lapsed"],
            "interview": out["interview"], "offer": out["offer"], "landed": out["landed"],
            "reached_interview": interviews, "submitted": sub["submitted"],
            "intent_only": sub["intent"], "legacy_ambiguous": sub["legacy-ambiguous"],
            "interviews_per_application": (round(interviews / applied, 3) if applied else 0.0)}


# --- BRIEF-051 (CR-rg-recruiter-outcomes): recruiter replies -> reviewable outcomes -------------
# Mirror of contracts/rolegenie/mail-outcomes.ts. A reply becomes a PROPOSAL; only the person's
# confirmation moves an application. Nothing here sends mail or reaches a network: the mail
# reader, classifier, labeler and locked state writer are all injected.

CLASSIFIER_KIND = {"rejection": "rejection", "interview-invite": "interview",
                   "acknowledgement": "confirmation"}
MIN_CLASSIFIER_CONFIDENCE = 0.6
COMPETING_MARGIN = 0.5
MAIL_EVIDENCE_PREFIX = "mail:"
HUMAN_APPLIED_EVIDENCE = "human-applied"
LABEL_UNAVAILABLE = "label-interface-unavailable"
MAX_THREADS = 50


def _parse_date(value) -> str | None:
    try:
        return dt.datetime.fromisoformat(str(value)[:19]).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def normalize_message(raw: dict) -> dict:
    """One connector message -> bounded untrusted text plus a validated date (None if bad/absent)."""
    raw = raw if isinstance(raw, dict) else {}
    pick = lambda *keys: next((raw[k] for k in keys if isinstance(raw.get(k), (str, int))), "")  # noqa: E731
    return {"id": str(pick("id", "messageId", "threadId"))[:200],
            "sender": str(pick("sender", "from"))[:200],
            "subject": str(pick("subject"))[:300],
            "body": str(pick("body", "snippet", "text"))[:2000],
            "date": _parse_date(pick("date", "receivedAt", "internalDate"))}


def read_threads(runner) -> dict:
    """Connector adapter. `runner()` returns the connector's JSON text (production: `claude -p` with
    ToolSearch + the Gmail search_threads tool, read-only, as apply.py verify does). Never raises
    and never writes: failures come back as actionable `problems`."""
    if runner is None:
        return {"messages": [], "problems": ["connector-missing"]}
    try:
        text = runner()
    except FileNotFoundError:
        return {"messages": [], "problems": ["connector-missing"]}
    except PermissionError:
        return {"messages": [], "problems": ["connector-permission"]}
    except Exception:  # noqa: BLE001
        return {"messages": [], "problems": ["connector-bad-response"]}
    try:
        text = str(text)
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        data = json.JSONDecoder().raw_decode(text[min(starts):])[0]
    except (ValueError, TypeError):
        return {"messages": [], "problems": ["connector-bad-response"]}
    items = data if isinstance(data, list) else (data.get("threads") or data.get("messages") or []) \
        if isinstance(data, dict) else []
    msgs = [normalize_message(i) for i in items[:MAX_THREADS] if isinstance(i, dict)]
    return {"messages": [m for m in msgs if m["id"]], "problems": []}


def proposal_decision(date_valid, scores, has_domain_match, label, confidence) -> tuple[str, list[str]]:
    if label is None:
        return "uncertain", ["classification-failed"]
    if label not in CLASSIFIER_KIND:
        return "ignored", ["not-an-outcome"]
    if not scores:
        return "ignored", ["unmatched"]
    reasons = []
    if not has_domain_match:
        reasons.append("weak-match")
    if not date_valid:
        reasons.append("no-date")
    top = sorted(scores, reverse=True)
    if len(top) > 1 and top[0] - top[1] < COMPETING_MARGIN:
        reasons.append("competing-applications")
    if (confidence or 0) < MIN_CLASSIFIER_CONFIDENCE:
        reasons.append("low-confidence")
    return ("uncertain" if reasons else "proposed"), reasons


def propose_outcome(message: dict, applications: list[dict], classify=None) -> dict:
    """Propose (never apply) which application a reply concerns and what it means. Message text
    is sanitized before it reaches the classifier and can never name an application: the uid comes
    only from the deterministic matcher over the person's own records."""
    clean = _sanitize_email(message["sender"], message["subject"], message["body"])
    date = message.get("date")
    try:
        m = match_email(clean["sender"], clean["subject"], clean["body"], date or "", applications, None)
    except Exception:  # noqa: BLE001
        m = {"candidates": []}
    cands = m["candidates"]
    label, conf, failure = None, None, None
    try:
        res = classify(clean["sender"], clean["subject"], clean["body"]) if classify else None
        if res:
            label, conf = res.get("label"), res.get("confidence")
    except Exception as exc:  # noqa: BLE001
        failure = str(exc)[:200]
    if classify is None:
        failure = "no-classifier"
    scored = [c for c in cands if c["signals"]["domain_match"]] or cands
    status, reasons = proposal_decision(bool(date), [c["score"] for c in scored],
                                        any(c["signals"]["domain_match"] for c in scored),
                                        None if (failure or label is None) else label, conf)
    best = scored[0]["uid"] if scored else None
    uid = best if status == "proposed" else None
    pid = hashlib.sha256(f"{message['id']}|{best or ''}".encode()).hexdigest()[:16]
    return {"id": pid, "messageId": message["id"], "status": status, "reasons": reasons,
            "kind": CLASSIFIER_KIND.get(label), "uid": uid, "confidence": conf,
            "candidates": [{"uid": c["uid"], "score": c["score"]} for c in scored],
            "subject": clean["subject"][:200], "date": date, "classifierError": failure,
            "labelStatus": None}


def add_proposal(ledger: dict, proposal: dict) -> dict:
    """Idempotent on proposal id: a re-delivered message keeps its first status."""
    out = {"proposals": dict(ledger.get("proposals", {}))}
    out["proposals"].setdefault(proposal["id"], dict(proposal))
    return out


def load_ledger(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("proposals"), dict) else {"proposals": {}}
    except (OSError, ValueError):
        return {"proposals": {}}


def save_ledger(path, ledger: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def confirm_proposal(ledger: dict, state: dict, pid: str, uid: str | None = None,
                     now: str | None = None) -> tuple[dict, dict]:
    """The person confirms a proposal. Returns (ledger, state) copies; raises ValueError (with an
    actionable message) when it cannot. A `proposed` proposal moves its own stored uid; an
    `uncertain` one needs a human-chosen `uid` that is one of its stored candidates. Idempotent:
    history that already carries `mail:<messageId>` is not applied twice (crash recovery)."""
    p = (ledger.get("proposals") or {}).get(pid)
    if not p:
        raise ValueError("unknown proposal")
    if p["status"] == "confirmed":
        return ledger, state
    if p["status"] not in ("proposed", "uncertain"):
        raise ValueError(f"a {p['status']} proposal cannot be confirmed")
    stored = [c["uid"] for c in p.get("candidates", [])]
    target = p["uid"] if p["status"] == "proposed" else uid
    if target is None or target not in stored:
        raise ValueError("choose one of the matched applications: " + (", ".join(stored) or "(none)"))
    if p["status"] == "uncertain" and p.get("kind") is None:
        raise ValueError("the reply was not classified; record the outcome by hand")
    app = (state.get(target) or {}).get("application")
    if not isinstance(app, dict):
        raise ValueError(f"{target} has no application record")
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    evidence = MAIL_EVIDENCE_PREFIX + p["messageId"]
    new_app = app
    if p["kind"] == "confirmation":
        events = list(app.get("mailEvents", []))
        if not any(e.get("messageId") == p["messageId"] for e in events):
            new_app = dict(app, mailEvents=events + [{"messageId": p["messageId"], "kind": "confirmation", "at": now}])
    elif not any(h.get("evidence") == evidence for h in app.get("history", [])):
        to = "rejected" if p["kind"] == "rejection" else "interview"
        new_app = transition(app, to, note=p.get("subject", ""), evidence=evidence, now=now)
    new_state = dict(state)
    new_state[target] = dict(state[target], application=new_app)
    new_ledger = {"proposals": dict(ledger["proposals"])}
    new_ledger["proposals"][pid] = dict(p, status="confirmed", uid=target, confirmedAt=now, labelStatus="pending")
    return new_ledger, new_state


def reject_proposal(ledger: dict, pid: str, now: str | None = None) -> dict:
    """The person declines a suggestion. Only the ledger changes; no outcome is touched."""
    p = (ledger.get("proposals") or {}).get(pid)
    if not p:
        raise ValueError("unknown proposal")
    if p["status"] == "confirmed":
        raise ValueError("a confirmed proposal cannot be rejected")
    out = {"proposals": dict(ledger["proposals"])}
    out["proposals"][pid] = dict(p, status="rejected", rejectedAt=now or dt.datetime.now().isoformat(timespec="seconds"))
    return out


def apply_confirmation(state_path, ledger_path, pid: str, now: str, update_state, uid: str | None = None) -> None:
    """Persist a confirmation: state first, inside the caller's lock (`update_state`, injected -
    rolegenie.update_state in production; outcomes never imports rolegenie), then the ledger
    atomically. A crash between the two leaves the proposal unconfirmed; a retry finds the
    `mail:` evidence, skips the transition and completes the ledger."""
    ledger = load_ledger(ledger_path)
    box = {}

    def change(state):
        box["ledger"], new_state = confirm_proposal(ledger, state, pid, uid=uid, now=now)
        state.update(new_state)
    update_state(Path(state_path), change)
    save_ledger(ledger_path, box["ledger"])


def pending_labels(ledger: dict) -> list[dict]:
    return [p for p in ledger.get("proposals", {}).values()
            if p.get("status") == "confirmed" and p.get("labelStatus") == "pending"]


def file_labels(ledger: dict, labeler) -> dict:
    """File the user's own label for each confirmed reply. `labeler(message_id, label)` is the
    connector's label operation (unverified, so injected); None -> an actionable result with the
    queue intact. Failures stay pending with the error; done is never refiled; nothing is sent."""
    if labeler is None:
        return {"ledger": ledger, "problem": LABEL_UNAVAILABLE}
    out = {"proposals": dict(ledger["proposals"])}
    for p in pending_labels(ledger):
        try:
            labeler(p["messageId"], p.get("kind") or "")
            out["proposals"][p["id"]] = dict(p, labelStatus="done", labelError=None)
        except Exception as exc:  # noqa: BLE001
            out["proposals"][p["id"]] = dict(p, labelError=str(exc)[:200])
    return {"ledger": out, "problem": None}


# --- submission honesty ---------------------------------------------------------------------

def confirm_applied(app: dict, now: str | None = None) -> dict:
    """The person says they submitted. One `human-applied` history event, once."""
    if any(h.get("evidence") == HUMAN_APPLIED_EVIDENCE for h in app.get("history", [])):
        return app
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    out = dict(app, submission="submitted")
    out["history"] = list(app.get("history", [])) + [
        {"state": app.get("outcome", "open"), "at": now, "note": "", "evidence": HUMAN_APPLIED_EVIDENCE}]
    return out


def submission_status(app: dict) -> str:
    """submitted | intent | legacy-ambiguous. Older records (no marker) are not retro-claimed."""
    if any(h.get("evidence") == HUMAN_APPLIED_EVIDENCE for h in app.get("history", [])) or app.get("ats"):
        return "submitted"
    return "intent" if app.get("submission") == "intent" else "legacy-ambiguous"
