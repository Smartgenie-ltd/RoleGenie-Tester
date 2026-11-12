"""Learning from the owner's decisions (arch.md §11; RG-S08).

Decisions become worked examples in the scoring prompt, and the scorer
predicts the owner's call before the owner makes it. Mirrors of
contracts/rolegenie/domain.ts asCall / firstCall / predictionOutcome.

    python rolegenie.py predict [--limit N]    # predictions for postings awaiting a call
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import rolegenie
import jev_scoring  # Jev-only re-score path (CR-rg-score-rollup); light import, no key/SDK at load

ROOT = Path(__file__).resolve().parent
CALLS = ("apply", "consider", "reject")
LEGACY = {"applied": "apply", "shortlisted": "consider", "ignored": "reject",
          "applied-externally": "apply"}  # applied externally = positive (CR-rg-applied-externally)
MAX_EXAMPLES = 40


def as_call(decision: str) -> str | None:
    return decision if decision in CALLS else LEGACY.get(decision)


def _opaque(text: str, limit: int = 120) -> str:
    """Flatten model/advert-derived text to a single opaque line for the learning
    prompt: no newlines or control characters, bounded length (review #6), so driver
    text cannot break the prompt's structure or carry injected instructions as lines."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit]


def driver_line(decision_kind: str, drivers: list[dict]) -> str:
    """Mirror of contracts/rolegenie/domain.ts driverLine: one learning-example line
    for a decision's ticked drivers, sign from the kind, each carrying its advert
    importance and match. Driver text is flattened to opaque data. Empty string when
    there are no drivers."""
    if not drivers:
        return ""
    verb = "blocked by" if decision_kind == "reject" else "drawn by"
    parts = [f"\"{_opaque(d.get('text'))}\" ({d.get('advertImportance', '')}, {d.get('match', '')})"
             for d in drivers]
    return f"{verb}: {'; '.join(parts)}"


def first_decision(history: list[dict]) -> dict | None:
    return next((h for h in history if as_call(h.get("decision", ""))), None)


def is_finalized(history: list[dict]) -> bool:
    """Mirror of isFinalized (CR-rg-archive): the owner has made a learning call OR archived
    the posting. Finalized postings leave review and are not re-scored or re-decomposed;
    archive is finalized but never a learning example (as_call returns None for it)."""
    return any(as_call(h.get("decision", "")) or h.get("decision") == "archive" for h in history)


def stale_since_processed(processed_advert: str | None, current_advert: str) -> bool:
    """CR-rg-heal-on-hydrate: the heal rule. A finalized role is normally frozen (its
    decision stands; it is not re-scored or re-decomposed). The one exception: a role
    processed (scored / decomposed) while its advert was a teaser and since hydrated
    carries a recorded processing fingerprint — the advert it was last processed against —
    that now DIFFERS from the full advert. Only then is it stale and reopened to self-heal.
    A missing fingerprint is NOT stale (a legacy finalized role is never reopened), and a
    master change alone (same advert) never reopens a finalized decision."""
    return bool(processed_advert) and processed_advert != current_advert


def prediction_outcome(prediction: dict | None, history: list[dict]) -> str:
    first = first_decision(history)
    if not prediction or not first:
        return "unmeasured"
    if prediction["at"] >= first["at"]:
        return "unmeasured"
    return "correct" if as_call(first["decision"]) == prediction["decision"] else "wrong"


def examples_block(state: dict[str, dict], skip: str = "", limit: int = MAX_EXAMPLES) -> str:
    """The owner's past first calls, newest first, as prompt text."""
    rows = []
    for uid, e in state.items():
        first = first_decision(e.get("decisions", []))
        if uid == skip or not first:
            continue
        call = as_call(first["decision"])
        # CR-rg-jd-decompose: structured drivers are the learning signal going
        # forward; a decision without them (older, or Consider) still contributes
        # its free-text reason so the scorer is not starved while the fork fills.
        drivers = first.get("drivers") or []
        if drivers:
            why = driver_line(call, drivers)
        else:
            reason = (first.get("note") or "").strip()
            reason = "" if reason == "from board" else reason
            why = f"Reason: {reason[:200]}" if reason else ""
        line = (f"- {call}: {(e.get('title') or '').strip()} — {e.get('board', '')}"
                f" ({e.get('location', '')})" + (f". {why}" if why else ""))
        rows.append((first["at"], line))
    if not rows:
        return "(No decisions recorded yet.)"
    rows.sort(reverse=True)
    return "\n".join(line for _, line in rows[:limit])


# --- Preference ordering (BRIEF-050, CR-rg-preference-learning; mirror of contracts/rolegenie/preferences.ts) ---
# The user's reasons/drivers reorder FUTURE jobs. Ordering only: fit, facts, requirements, exclusions and the scoring
# prompt are never touched and no job is dropped. Deterministic and keyless (no model call). 038 wires
# rank_by_preference into daily/board ordering; examples_block keeps first-call history for trust measurement.
POINTS_MAX = 3
MAX_TERMS = 12
STOPWORDS = frozenset(
    "with that this from have will your they their them were been being than then into over also more most much "
    "some such only very just about would could should role job work team want like good nice because which what "
    "when where while there these those other".split())


def _tokens(text) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"[a-z][a-z0-9+#.-]*", str(text or "").lower()):
        t = m.group(0).rstrip(".-")
        if len(t) >= 4 and t not in STOPWORDS and t not in out:
            out.append(t)
    return out


def terms_of(text) -> list[str]:
    """A decision's terms: filtered, de-duplicated in order of appearance, capped."""
    return _tokens(text)[:MAX_TERMS]


def _eligible(events: list) -> list[dict]:
    return [e for e in events if isinstance(e, dict) and as_call(e.get("decision", ""))
            and isinstance(e.get("at"), str) and e["at"]]


def _latest_event(history: list) -> dict | None:
    best = None
    for e in _eligible(history):
        if best is None or e["at"] >= best["at"]:  # equal `at`: the later list entry wins
            best = e
    return best


def latest_call(history: list) -> str | None:
    """The latest eligible call (owner ruling 2026-11-05): shapes future preferences. first_decision is untouched
    and stays the trust-measurement basis. Archive/unknown decisions and events without a string `at` are skipped."""
    e = _latest_event(history)
    return as_call(e["decision"]) if e else None


def _group_key(x: dict, uid: str) -> str:
    if x.get("url"):
        return f"url:{x['url']}"
    if x.get("title"):
        return f"tb:{str(x.get('board') or '').lower()}|{str(x['title']).lower()}"
    return f"uid:{uid}"


def _neutral(job: dict, reason: str = "no preference signal; fit order kept") -> dict:
    return {**job, "preference": {"points": 0, "reason": reason}}


def rank_by_preference(jobs: list[dict], state) -> dict:
    """Reorder `jobs` (given in existing fit order) by learned preference. Returns
    {"order": [job copies + "preference": {points, reason}], "fallback": None | message}. Sort key is
    (-points, original index): neutral/no history keeps the existing order. Points = clamp(sum of weights of the
    distinct profile terms found in the job's title + requirement texts, -3, 3). One signal per posting (grouped
    by url, else board+title, else uid) from its latest eligible call; consider teaches nothing; a job the user
    already decided, or an excluded one, holds 0 points. Malformed input never raises: the order is the input order
    with a visible fallback message."""
    try:
        if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
            raise ValueError("jobs malformed")
        if state is not None and not isinstance(state, dict):
            raise ValueError("decision state malformed")
        groups: dict[str, list] = {}
        for uid, e in (state or {}).items():
            if not isinstance(e, dict) or not isinstance(e.get("decisions", []), list):
                raise ValueError("decision entry malformed")
            groups.setdefault(_group_key(e, uid), []).extend(e.get("decisions", []))
        weight: dict[str, list] = {}  # term -> [net weight, source]
        for events in groups.values():
            ev = _latest_event(events)
            call = as_call(ev["decision"]) if ev else None
            if call not in ("apply", "reject"):
                continue
            sign = 1 if call == "apply" else -1
            note = str(ev.get("note") or "").strip()
            from_reason = [] if note == "from board" else _tokens(note)
            from_drivers = _tokens(" ".join(str((d or {}).get("text") or "") for d in (ev.get("drivers") or [])
                                            if isinstance(d, dict)))
            for t in ([*from_reason, *[t for t in from_drivers if t not in from_reason]])[:MAX_TERMS]:
                cur = weight.setdefault(t, [0, "reason" if t in from_reason else "driver"])
                cur[0] += sign
        ranked = []
        for idx, job in enumerate(jobs):
            own = groups.get(_group_key(job, str(job.get("uid"))))
            if own is None and isinstance(state, dict) and isinstance(state.get(job.get("uid")), dict):
                own = state[job["uid"]].get("decisions", [])
            decided = latest_call(own or [])
            if decided:
                ranked.append((0, idx, _neutral(job, f"you already decided: {decided}")))
                continue
            if job.get("excluded"):
                ranked.append((0, idx, _neutral(job, "excluded by a hard filter; preference not applied")))
                continue
            text = " ".join([str(job.get("title") or ""),
                             *[str((r or {}).get("text") or "") for r in (job.get("requirements") or [])
                               if isinstance(r, dict)]])
            matched = [t for t in _tokens(text) if weight.get(t, [0])[0] != 0]
            points = max(-POINTS_MAX, min(POINTS_MAX, sum(weight[t][0] for t in matched)))
            if points == 0:
                ranked.append((0, idx, _neutral(job)))
                continue
            matched.sort(key=lambda t: (-abs(weight[t][0]), t))
            parts = [f'toward "{t}" (you applied; {weight[t][1]})' if weight[t][0] > 0
                     else f'away from "{t}" (you rejected; {weight[t][1]})' for t in matched[:3]]
            head = f"+{points}" if points > 0 else str(points)
            ranked.append((points, idx, {**job, "preference": {"points": points,
                                                              "reason": f"preference {head}: {'; '.join(parts)}"}}))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        return {"order": [r[2] for r in ranked], "fallback": None}
    except Exception as exc:  # noqa: BLE001 - never raise into the board; fall back visibly
        order = [dict(j) for j in jobs] if isinstance(jobs, list) and all(isinstance(j, dict) for j in jobs) else []
        return {"order": order,
                "fallback": f"preference ordering unavailable: {exc}; showing existing fit order"}


def cmd_predict(argv: list[str], cfg: dict) -> int:
    """Record predictions (and advert facts) for scored postings still awaiting the
    owner's first call. Scores are not changed."""
    import tailor

    ap = argparse.ArgumentParser(prog="rolegenie.py predict")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args(argv)
    state_path = ROOT / cfg["output"]["state_file"]
    state = rolegenie.load_state(state_path)
    cmd = rolegenie.model_command(cfg)
    if not cmd:
        print("No model CLI available", file=sys.stderr)
        return 1
    uids = [u for u, e in state.items() if isinstance(e.get("score"), int) and not e.get("prediction")
            and not is_finalized(e.get("decisions", []))][:args.limit]
    missing = [u for u in uids if not tailor.jd_path(u).exists()]
    if missing:
        tailor.fetch_missing_jds(missing, state, cfg)
    profile = rolegenie.build_profile(cfg)
    done = 0
    for uid in uids:
        if not tailor.jd_path(uid).exists():
            continue
        e = state[uid]
        p = rolegenie.Posting(uid=uid, board=e.get("board", ""), title=e.get("title", ""),
                              location=e.get("location", ""), url=e.get("url", ""),
                              body=tailor.jd_path(uid).read_text(encoding="utf-8"),
                              zone=e.get("zone", "uk"))
        # CR-rg-score-from-requirements: predict on the same basis as real scoring.
        import board
        p.requirements = board.requirements_record(uid, e)
        if not rolegenie.score_postings([p], cfg, examples=examples_block(state, skip=uid), profile=profile) or p.score is None:
            continue

        def keep(st: dict, u=uid, q=p) -> None:
            st[u]["prediction"] = q.prediction
            st[u]["facts"] = q.facts
            if q.excluded:
                st[u]["excluded"] = q.excluded
        rolegenie.update_state(state_path, keep)
        done += 1
        print(f"  {uid}: predicts {p.prediction.get('decision')}", file=sys.stderr)
    print(f"Predicted {done} of {len(uids)}", file=sys.stderr)
    return 0


def cmd_rescore(argv: list[str], cfg: dict) -> int:
    """Re-score undecided postings against the CURRENT master (CR-rg-rescore),
    WITHOUT tailoring or decomposing. The fit score is computed once at first-seen
    against whatever master was then in force; enriching the master leaves older
    scores stale.

    By default this is a JEV-ONLY re-score (CR-rg-score-rollup): it refreshes the
    fit score (score / score100 / jev_match) from the STORED decomposition against
    the current certified card, WITHOUT the LLM call, and KEEPS the last full
    score's facts/verdict/ceiling (the advert is unchanged; only the master moved).
    This is the cheap standing norm for a master change — seconds and pennies, no
    LLM round-trip. `--full` re-runs the full LLM fact-extraction AND the Jev score,
    for when the ADVERT changed too (e.g. a heal-on-hydrate sweep) so facts are
    also stale. No fallback (owner's ruling): if the card or Jev is unavailable the
    posting is left unchanged, never scored by an LLM guess.

    By default only stale, still-undecided postings are re-scored (idempotent: a
    posting already scored on the current master is skipped); --force re-scores
    regardless. --min/--max bound the score range to re-score. After scoring, the
    under-6 review-threshold exclusion is re-applied: a posting that rises to the
    threshold is released into review, one still below it is excluded (a hard
    exclusion always wins)."""
    import tailor

    ap = argparse.ArgumentParser(prog="rolegenie.py rescore")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--min", type=int, default=1, help="lowest current score to re-score")
    ap.add_argument("--max", type=int, default=10, help="highest current score to re-score")
    ap.add_argument("--force", action="store_true",
                    help="re-score even postings already scored on the current master")
    ap.add_argument("--full", action="store_true",
                    help="re-run the full LLM fact-extraction AND the Jev fit score (use when "
                         "the ADVERT changed, e.g. after hydration); the default refreshes the "
                         "Jev fit score only and keeps the stored facts/verdict/ceiling")
    args = ap.parse_args(argv)
    state_path = ROOT / cfg["output"]["state_file"]
    state = rolegenie.load_state(state_path)
    cmd = rolegenie.model_command(cfg)
    if not cmd:
        print("No model CLI available", file=sys.stderr)
        return 1
    current = tailor.current_master_sha(cfg)
    # Jev-only (default): the certified card is the scoring state, read once per run.
    # No fallback (owner's ruling): if it cannot be read, nothing is re-scored.
    card = None
    if not args.full:
        try:
            card = rolegenie.master_card(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"certified card unavailable, cannot Jev-re-score: {exc}", file=sys.stderr)
            return 1

    def needs(u: str, e: dict) -> bool:
        if not isinstance(e.get("score"), int):
            return False
        if not (args.min <= e["score"] <= args.max):
            return False
        jp = tailor.jd_path(u)
        if not jp.exists():
            return False
        # CR-rg-hydrate-gate: an excerpt is never re-scored here. Hydration
        # (CR-rg-hydrate-fetch) fills the full advert and re-scores it; re-scoring a
        # teaser would just reproduce a wrong score.
        body = jp.read_text(encoding="utf-8")
        if not rolegenie.is_hydrated(body):
            return False
        if is_finalized(e.get("decisions", [])):
            # CR-rg-heal-on-hydrate: a finalized role is normally skipped, but one decided
            # while its advert was a teaser and since hydrated is stale and must self-heal
            # on the full advert (decision history untouched). --force never reopens it;
            # only the advert transition does.
            return stale_since_processed(e.get("scored_advert"), rolegenie.advert_sha(body))
        return args.force or rolegenie.master_stale(e.get("scored_master"), current)

    # Lowest-scoring first, so a bounded run clears the borderline band the master
    # change is most likely to move (the owner's 4s and 5s).
    candidates = sorted((ue for ue in state.items() if needs(*ue)),
                        key=lambda ue: (ue[1].get("score") or 0))
    uids = [u for u, _ in candidates][:args.limit]
    profile = rolegenie.build_profile(cfg)
    moved, done = 0, 0
    import board
    for uid in uids:
        e = state[uid]
        body = tailor.jd_path(uid).read_text(encoding="utf-8")
        p = rolegenie.Posting(uid=uid, board=e.get("board", ""), title=e.get("title", ""),
                              location=e.get("location", ""), url=e.get("url", ""),
                              body=body, zone=e.get("zone", "uk"),
                              listed_contract=e.get("listed_contract", False))
        # CR-rg-score-from-requirements: score against the stored requirement list (the
        # loader returns [] when the decomposition is stale/absent).
        p.requirements = board.requirements_record(uid, e)
        before = e.get("score")
        if args.full:
            # Full re-score: LLM facts/verdict/ceiling + Jev fit (score_postings falls back
            # to the advert text when the requirement list is empty).
            if not rolegenie.score_postings([p], cfg, examples=examples_block(state, skip=uid),
                                            profile=profile) or p.score is None:
                print(f"  {uid}: re-score failed, left unchanged", file=sys.stderr)
                continue
        else:
            # Jev-only (default): refresh the fit score from the stored decomposition against
            # the current card, WITHOUT the LLM call. Facts/verdict/ceiling/category are kept
            # from the last full score and written back unchanged by keep() below.
            if len(p.requirements) < jev_scoring.THIN_MIN_REQUIREMENTS:
                why = ("no up-to-date decomposition (run `decompose` first)" if not p.requirements
                       else f"thin decomposition ({len(p.requirements)} < "
                            f"{jev_scoring.THIN_MIN_REQUIREMENTS} requirements)")
                print(f"  {uid}: skipped — {why}", file=sys.stderr)
                continue
            try:
                s10, r100, labels = jev_scoring.score_requirements(card, p.requirements)
            except jev_scoring.JevUnavailable as exc:
                print(f"  {uid}: Jev unavailable ({exc}), left unchanged", file=sys.stderr)
                continue
            p.score = s10
            p.score100 = round(r100, 1)
            p.jev_match = labels
            p.scored_master = current
            p.scored_advert = rolegenie.advert_sha(body)  # CR-rg-heal-on-hydrate (advert unchanged)
            p.facts = e.get("facts") or {}              # kept from the last full score
            p.verdict = e.get("verdict", "")
            p.category = e.get("category", "")
            p.ceiling = e.get("ceiling")
            p.ceiling_gaps = e.get("ceiling_gaps") or []
            # A hard exclusion (zone/language/quota) is recomputed from the kept facts; the
            # review threshold re-applies below. A high score never clears a hard exclusion.
            p.excluded = rolegenie.exclusion_reason(p.zone, p.facts)
        # Re-apply the review threshold: a hard exclusion wins; otherwise below-6 is
        # excluded, at/above cleared.
        if not p.excluded:
            p.excluded = rolegenie.below_threshold_reason(p.score)

        def keep(st: dict, u=uid, q=p, cur=current) -> None:
            if u not in st:
                return
            # score100 + jev_match persisted on the re-score path (BRIEF-015): the switch's
            # new fields must survive a re-score, not only a first scan (CR-rg-score-rollup).
            st[u].update({"score": q.score, "score100": q.score100, "jev_match": q.jev_match,
                          "verdict": q.verdict, "facts": q.facts, "excluded": q.excluded,
                          "category": q.category, "ceiling": q.ceiling,
                          "ceiling_gaps": q.ceiling_gaps, "scored_master": cur,
                          "scored_advert": q.scored_advert})  # CR-rg-heal-on-hydrate
            if q.prediction:
                st[u]["prediction"] = q.prediction
        rolegenie.update_state(state_path, keep)
        done += 1
        moved += 1 if before != p.score else 0
        tag = "excluded" if p.excluded else "in review"
        print(f"  {uid}: {before} -> {p.score} ({tag})", file=sys.stderr)
    mode = "full LLM+Jev" if args.full else "Jev-only"
    print(f"Re-scored {done} of {len(uids)} ({mode}); {moved} changed score", file=sys.stderr)
    return 0


def cmd_decompose(argv: list[str], cfg: dict) -> int:
    """Backfill (CR-rg-jd-decompose): decompose scored, undecided postings that have
    no up-to-date requirements yet, WITHOUT changing their scores. Idempotent — a
    posting whose decomposition already matches its stored advert is skipped."""
    import tailor

    ap = argparse.ArgumentParser(prog="rolegenie.py decompose")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--force", action="store_true",
                    help="re-decompose even postings already done (e.g. after a method change)")
    args = ap.parse_args(argv)
    state_path = ROOT / cfg["output"]["state_file"]
    state = rolegenie.load_state(state_path)
    cmd = rolegenie.decompose_command(cfg)  # cheap/local model (ENG-032)
    if not cmd:
        print("No model CLI available", file=sys.stderr)
        return 1

    def jd_text(u: str) -> str:
        p = tailor.jd_path(u)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def record_sha(u: str) -> str | None:
        p = tailor.jd_decomp_path(u)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            return rec.get("advert_sha") if isinstance(rec, dict) else None
        except (ValueError, OSError):
            return None  # a corrupt record is treated as absent, so it is rebuilt

    def needs(u: str, e: dict) -> bool:
        if not isinstance(e.get("score"), int):
            return False
        body = jd_text(u)
        if not body:
            return False
        # CR-rg-hydrate-gate: an excerpt is not decomposed (decompose_postings would
        # only mark it pending-hydration); skip it until hydration fills the advert.
        if not rolegenie.is_hydrated(body):
            return False
        d = e.get("decompose") or {}
        body_sha = rolegenie.advert_sha(body)
        if is_finalized(e.get("decisions", [])):
            # CR-rg-heal-on-hydrate: a finalized role is normally frozen, but one whose
            # advert has hydrated since it was decomposed (its recorded decomposition
            # predates the current advert) is stale and must be re-decomposed. A finalized
            # role whose advert is unchanged, or which was never decomposed, stays skipped,
            # and --force never reopens a finalized decision.
            return stale_since_processed(d.get("advert_sha"), body_sha)
        if args.force:
            return True  # re-decompose regardless of an existing record
        if d.get("advert_sha") != body_sha:
            return True  # never decomposed, or the advert changed since
        if d.get("status") == "thin":
            return False  # correctly suppressed for this advert; nothing to do until the JD changes
        # 'ok' is trusted only when the record file genuinely parses and matches the
        # advert (review #8) — a missing, corrupt or stale record is rebuilt.
        return not (d.get("status") == "ok" and record_sha(u) == body_sha)

    # Highest-scoring undecided postings first, so "--limit 10" is the top 10.
    candidates = sorted((ue for ue in state.items() if needs(*ue)),
                        key=lambda ue: -(ue[1].get("score") or 0))
    uids = [u for u, _ in candidates][:args.limit]
    profile = rolegenie.build_profile(cfg)
    done = 0
    for uid in uids:
        e = state[uid]
        p = rolegenie.Posting(uid=uid, board=e.get("board", ""), title=e.get("title", ""),
                              location=e.get("location", ""), url=e.get("url", ""), body=jd_text(uid),
                              category=e.get("category", ""), ceiling=e.get("ceiling"),
                              facts=e.get("facts") or {})
        rolegenie.decompose_postings([p], cfg, profile=profile, cmd=cmd)
        tailor.write_decomposition(p)

        def keep(st: dict, u=uid, q=p) -> None:
            if u in st:
                st[u]["decompose"] = q.decompose
        rolegenie.update_state(state_path, keep)
        done += 1 if p.requirements else 0
        print(f"  {uid}: {(p.decompose or {}).get('count', 0)} requirements", file=sys.stderr)
    print(f"Decomposed {done} of {len(uids)}", file=sys.stderr)
    return 0


def cmd_hydrate(argv: list[str], cfg: dict) -> int:
    """Hydrate held Adzuna postings (CR-rg-hydrate-fetch): the hydration STAGE, built to
    the agentic stage contract. For each held posting (a teaser JD on disk, an Adzuna
    detail URL): validate_input (held + has URL) -> transform (headless Playwright fetch)
    -> correctness-check (rolegenie.hydration_problems) -> persist (write the full advert
    to jd/<uid> and set hydrated=True) ONLY on a clean check. A failed fetch or failed
    check holds the posting with a reason (expired / needs-assisted / failed) and never
    writes a bad advert. Does NOT score or decompose — those are separate stages
    (run `rescore` / `decompose` after). Idempotent: an already-hydrated posting is skipped."""
    import time

    import tailor

    ap = argparse.ArgumentParser(prog="rolegenie.py hydrate")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--uid", help="hydrate this posting only")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between fetches (be polite)")
    args = ap.parse_args(argv)
    state_path = ROOT / cfg["output"]["state_file"]
    state = rolegenie.load_state(state_path)

    import hydrate_fetch

    def held(u: str, e: dict) -> bool:
        # Both Adzuna URL forms (details and land/ad) normalise to a hydratable detail URL.
        if hydrate_fetch.details_url(e.get("url", "")) is None:
            return False
        jp = tailor.jd_path(u)
        body = jp.read_text(encoding="utf-8") if jp.exists() else ""
        return not rolegenie.is_hydrated(body)

    uids = [args.uid] if args.uid else [u for u, e in state.items() if held(u, e)][:args.limit]
    print(f"Hydrating {len(uids)} held Adzuna posting(s) with Playwright", file=sys.stderr)

    def set_hydrated(st: dict, u: str) -> None:
        if u in st:
            st[u]["hydrated"] = True
            st[u].pop("hydration", None)  # clear any prior hold
            # The score was computed on the teaser and is provisional; invalidate its
            # master fingerprint so `rescore` re-does it on the full advert.
            st[u]["scored_master"] = ""

    def hold(st: dict, u: str, status: str, reason: str) -> None:
        if u in st:
            st[u]["hydrated"] = False
            st[u]["hydration"] = {"status": status, "reason": reason, "at": now()}

    done = failed = 0
    for uid in uids:
        url = hydrate_fetch.details_url(state[uid].get("url", "")) or state[uid].get("url", "")
        try:
            body, http_status = hydrate_fetch.fetch_advert(url)
        except Exception as exc:  # noqa: BLE001 - one bad fetch must not stop the sweep
            print(f"  {uid}: fetch failed ({str(exc)[:80]})", file=sys.stderr)
            rolegenie.update_state(state_path, lambda st, u=uid, r=str(exc)[:160]: hold(st, u, "failed", r))
            failed += 1
            time.sleep(args.delay)
            continue
        problems = rolegenie.hydration_problems(body, http_status)
        if problems:
            reason = "; ".join(problems)[:200]
            kind = "expired" if any("HTTP" in p for p in problems) else "needs-assisted"
            print(f"  {uid}: held — {reason}", file=sys.stderr)
            rolegenie.update_state(state_path, lambda st, u=uid, k=kind, r=reason: hold(st, u, k, r))
            failed += 1
        else:
            tailor.jd_path(uid).write_text(body, encoding="utf-8")
            rolegenie.update_state(state_path, lambda st, u=uid: set_hydrated(st, u))
            print(f"  {uid}: hydrated ({len(body)} chars)", file=sys.stderr)
            done += 1
        time.sleep(args.delay)
    print(f"Hydrated {done}, held/failed {failed}, of {len(uids)}", file=sys.stderr)
    return 0


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
