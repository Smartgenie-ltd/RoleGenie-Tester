"""Slice 1 onboarding (BRIEF-022, CR-rg-slice1-assessment; contract: contracts/rolegenie/assessment.ts).

A new user's resumes -> one master CV whose every fact is a verified verbatim span (or the user's own confirmation),
with contradictions flagged for the user to resolve, then approved by the user. Self-ratings and denials are stored
apart from the master. All user data lives in ONE per-user directory outside the clone: $ROLEGENIE_HOME, else
~/.rolegenie. Every write is a temp file renamed into place, so no artefact is ever half-written.

    rolegenie.py onboard FILE...            rolegenie.py master show
    rolegenie.py master resolve CID (--keep FID | --ack)
    rolegenie.py master confirm "TEXT" [--category C]
    rolegenie.py master approve             rolegenie.py rate "CAPABILITY" 1-5
    rolegenie.py deny "CAPABILITY"
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "consolidate_v1.md"
FACT_CATEGORIES = ("role", "skill", "achievement", "domain", "certification", "education", "other")
CONTRADICTION_KINDS = ("dates", "title", "employer")
READABLE = (".txt", ".md", ".docx", ".pdf")


class ResumeUnreadable(Exception):
    """A resume file could not be read; it is named and recorded, never dropped silently."""


class ResumeSourceCollision(Exception):
    """Two resumes share a file name; a source is identified by it, so one would silently replace the other."""


class ModelUnavailable(Exception):
    """Claude could not answer (CLI missing, failed, timed out, or returned no JSON). No fallback."""


class ContradictionOpen(Exception):
    """Approval refused: a contradiction is still open."""


class MasterNotApproved(Exception):
    """Assessment refused: no master, no approval, or the master changed since approval."""


# --- data dir and atomic writes ---

def data_dir() -> Path:
    return Path(os.environ.get("ROLEGENIE_HOME") or Path.home() / ".rolegenie")


def load(name: str, default):
    p = data_dir() / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name: str, obj) -> Path:
    """Write JSON atomically: a temp file in the same directory, then os.replace (atomic on Windows and Linux)."""
    p = data_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def master_sha(master: dict) -> str:
    return hashlib.sha256(json.dumps(master, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --- contract mirror (assessment.ts) ---

def norm(s: str) -> str:
    return " ".join((s or "").split())


def span_verified(span: str, text: str) -> bool:
    s = norm(span)
    return bool(s) and s in norm(text)


def approval_problems(sha: str, contradictions: list[dict], approval: dict | None) -> list[str]:
    problems = []
    if not approval:
        problems.append("master not approved")
    elif approval.get("masterSha") != sha:
        problems.append("master changed since approval")
    problems += [f"contradiction {c['id']} is open" for c in contradictions if c.get("status") == "open"]
    return problems


# --- reading resumes ---

def read_resume(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ResumeUnreadable(f"{path.name}: file not found")
    ext = path.suffix.lower()
    if ext not in READABLE:
        raise ResumeUnreadable(f"{path.name}: unsupported type '{ext}' (use {', '.join(READABLE)})")
    try:
        if ext == ".docx":
            import docx
            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs]
            parts += [c.text for t in d.tables for row in t.rows for c in row.cells]
            text = "\n".join(parts)
        elif ext == ".pdf":
            import pypdf
            text = "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages)
        else:
            text = path.read_bytes().decode("utf-8-sig")
    except Exception as exc:  # noqa: BLE001 - any parser failure is a named unreadable resume
        raise ResumeUnreadable(f"{path.name}: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not text.strip():
        raise ResumeUnreadable(f"{path.name}: no text could be extracted")
    return text


def source_collisions(paths) -> list[str]:
    """Mirror of sourceNameCollisions (contracts/rolegenie/assessment.ts): ambiguous names, once each, first-seen order."""
    seen, dup = {}, []
    for p in paths:
        name = re.split(r"[\\/]", str(p))[-1]
        if name.lower() not in seen:
            seen[name.lower()] = name
        elif seen[name.lower()] not in dup:
            dup.append(seen[name.lower()])
    return dup


def refuse_collisions(paths) -> None:
    dup = source_collisions(paths)
    if dup:
        raise ResumeSourceCollision(f"more than one resume is named {', '.join(dup)}; a resume is identified by its "
                                    "file name, so rename one so every resume file name is different, then run again")


# --- the Claude call (D4: Claude explicitly, never the configured nightly scorer) ---

def call_model(prompt: str, cfg: dict) -> dict:
    import rolegenie
    cmd = rolegenie.model_command(cfg, "claude")
    if not cmd:
        raise ModelUnavailable("the claude CLI is not on PATH (install Claude Code and log in)")
    try:
        return rolegenie.parse_model_json(rolegenie.run_model(prompt, cfg, timeout=900, cmd=cmd))
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ModelUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc


_call_model_real = call_model


def consolidation_prompt(sources: list[dict]) -> str:
    body = "\n\n".join(f"=== FILE: {s['file']} ===\n{s['extractedText']}\n=== END FILE: {s['file']} ==="
                       for s in sources)
    return PROMPT_PATH.read_text(encoding="utf-8") + "\n" + body + "\n"


def build_master(sources: list[dict], unreadable: list[dict], parsed: dict) -> dict:
    """Verify every span in code. A fact's text is its first verified span; a fact with none is rejected."""
    refuse_collisions([s["file"] for s in sources])
    texts = {s["file"]: s["extractedText"] for s in sources}
    facts, rejected, index_to_id = [], [], {}
    for i, f in enumerate(parsed.get("facts") or []):
        if not isinstance(f, dict):
            continue
        raw = [s for s in f.get("sources") or [] if isinstance(s, dict)]
        good = [{"file": s["file"], "span": norm(s["span"])} for s in raw
                if isinstance(s.get("file"), str) and isinstance(s.get("span"), str)
                and s["file"] in texts and span_verified(s["span"], texts[s["file"]])]
        cat = f.get("category") if f.get("category") in FACT_CATEGORIES else "other"
        if not good:
            rejected.append({"category": cat, "sources": raw, "reason": "no span found verbatim in its resume"})
            continue
        fid = f"f{len(facts) + 1}"
        index_to_id[i] = fid
        facts.append({"id": fid, "text": good[0]["span"], "category": cat, "sources": good})
    contradictions = []
    for c in parsed.get("contradictions") or []:
        if not isinstance(c, dict):
            continue
        ids = [index_to_id[i] for i in c.get("facts") or [] if type(i) is int and i in index_to_id]
        if not ids:  # every side was rejected: still report it, never drop silently
            rejected.append({"category": "contradiction", "sources": [], "reason": f"contradiction on rejected facts: "
                             f"{str(c.get('note', ''))[:200]}"})
            continue
        contradictions.append({"id": f"c{len(contradictions) + 1}", "factIds": ids,
                               "kind": c.get("kind") if c.get("kind") in CONTRADICTION_KINDS else "dates",
                               "note": str(c.get("note") or "")[:300], "status": "open"})
    return {"sources": sources, "unreadable": unreadable, "facts": facts,
            "contradictions": contradictions, "rejected": rejected}


# --- commands ---

def _out():
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def fail(exc: Exception) -> int:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def cmd_onboard(argv: list[str], cfg: dict) -> int:
    _out()
    if not argv:
        print("usage: rolegenie.py onboard RESUME_FILE...", file=sys.stderr)
        return 2
    try:
        refuse_collisions(argv)
    except ResumeSourceCollision as exc:
        print("Nothing read and nothing written.", file=sys.stderr)
        return fail(exc)
    sources, unreadable = [], []
    for a in argv:
        p = Path(a)
        try:
            text = read_resume(p)
        except ResumeUnreadable as exc:
            fail(exc)
            unreadable.append({"file": p.name, "reason": str(exc)})
            continue
        sources.append({"file": p.name, "sha": hashlib.sha256(p.read_bytes()).hexdigest()[:16],
                        "extractedText": text})
    if not sources:
        print("ResumeUnreadable: no resume could be read; nothing written", file=sys.stderr)
        return 1
    try:
        parsed = call_model(consolidation_prompt(sources), cfg)
    except ModelUnavailable as exc:
        print("No master written.", file=sys.stderr)
        return fail(exc)
    m = build_master(sources, unreadable, parsed)
    path = save("master.json", m)
    print(f"Master draft written: {path}")
    print(f"  {len(m['facts'])} facts from {len(sources)} resume(s); {len(m['contradictions'])} contradiction(s) "
          f"to review; {len(m['rejected'])} rejected; {len(unreadable)} unreadable")
    for r in m["rejected"]:
        spans = "; ".join(s.get("span", "") if isinstance(s, dict) else "" for s in r["sources"])
        print(f"  REJECTED ({r['reason']}): {spans[:200]}")
    for u in unreadable:
        print(f"  UNREADABLE: {u['reason']}")
    print("Next: rolegenie.py master show, resolve each contradiction, then rolegenie.py master approve")
    return 0


def show(m: dict) -> None:
    ap = load("approval.json", None)
    probs = approval_problems(master_sha(m), m["contradictions"], ap)
    print("APPROVED" if not probs else "NOT APPROVED: " + "; ".join(probs))
    print(f"Sources: {', '.join(s['file'] for s in m['sources'])}")
    for u in m.get("unreadable") or []:
        print(f"Unreadable source: {u['reason']}")
    print("\nFacts:")
    for f in m["facts"]:
        print(f"  {f['id']} [{f['category']}] {f['text']}")
        for s in f["sources"]:
            print(f"      source: {s['file']}")
        if f.get("userConfirmation"):
            print(f"      confirmed by you at {f['userConfirmation']['confirmedAt']}")
    if m["contradictions"]:
        print("\nContradictions:")
        for c in m["contradictions"]:
            extra = f", kept {c['keep']}" if c.get("keep") else ""
            print(f"  {c['id']} [{c['kind']}] {c['status']}{extra}: {c['note']}")
            for fid in c["factIds"]:
                f = next(x for x in m["facts"] if x["id"] == fid)
                print(f"      {fid}: {f['text']}")
    if m.get("rejected"):
        print(f"\nRejected (not in the master): {len(m['rejected'])}")
        for r in m["rejected"]:
            spans = "; ".join(s.get("span", "") if isinstance(s, dict) else "" for s in r["sources"])
            print(f"  {r['reason']}: {spans[:200]}")


def cmd_master(argv: list[str], cfg: dict) -> int:
    _out()
    sub = argv[0] if argv else ""
    m = load("master.json", None)
    if m is None:
        print("no master yet: run rolegenie.py onboard RESUME_FILE... first", file=sys.stderr)
        return 1
    if sub == "show":
        show(m)
        return 0
    if sub == "resolve" and len(argv) >= 3:
        c = next((x for x in m["contradictions"] if x["id"] == argv[1]), None)
        if c is None:
            print(f"no contradiction {argv[1]}", file=sys.stderr)
            return 1
        if argv[2] == "--ack":
            c["status"] = "acknowledged"
        elif argv[2] == "--keep" and len(argv) >= 4 and argv[3] in c["factIds"]:
            c["status"], c["keep"] = "resolved", argv[3]
        else:
            print(f"use --keep with one of {', '.join(c['factIds'])}, or --ack", file=sys.stderr)
            return 1
        save("master.json", m)
        print(f"{c['id']} {c['status']}")
        return 0
    if sub == "confirm" and len(argv) >= 2 and argv[1].strip():
        cat = argv[3] if len(argv) >= 4 and argv[2] == "--category" else "skill"
        if cat not in FACT_CATEGORIES:
            print(f"category must be one of {', '.join(FACT_CATEGORIES)}", file=sys.stderr)
            return 1
        fid = f"f{max([int(f['id'][1:]) for f in m['facts']] or [0]) + 1}"
        m["facts"].append({"id": fid, "text": argv[1].strip(), "category": cat, "sources": [],
                           "userConfirmation": {"confirmedAt": now()}})
        save("master.json", m)
        print(f"{fid} added as your confirmed fact; approve the master again before assessing")
        return 0
    if sub == "approve":
        open_ = [c["id"] for c in m["contradictions"] if c["status"] == "open"]
        if open_:
            return fail(ContradictionOpen(f"resolve or acknowledge {', '.join(open_)} first "
                                          "(rolegenie.py master resolve CID --keep FID | --ack)"))
        save("approval.json", {"masterSha": master_sha(m), "approvedAt": now()})
        print(f"Master approved ({len(m['facts'])} facts).")
        return 0
    print('usage: rolegenie.py master show | resolve CID (--keep FID | --ack) | confirm "TEXT" [--category C] '
          "| approve", file=sys.stderr)
    return 2


def cmd_rate(argv: list[str], cfg: dict) -> int:
    if len(argv) != 2 or argv[1] not in {"1", "2", "3", "4", "5"} or not argv[0].strip():
        print('usage: rolegenie.py rate "CAPABILITY" 1-5', file=sys.stderr)
        return 1
    ratings = load("ratings.json", {})
    ratings[argv[0].strip()] = int(argv[1])
    save("ratings.json", ratings)
    print(f"rated {argv[0].strip()} {argv[1]}/5 (kept apart from evidence; never changes fit)")
    return 0


def cmd_deny(argv: list[str], cfg: dict) -> int:
    if len(argv) != 1 or not argv[0].strip():
        print('usage: rolegenie.py deny "CAPABILITY"', file=sys.stderr)
        return 1
    denials = [d for d in load("denials.json", []) if d["capability"] != argv[0].strip()]
    denials.append({"capability": argv[0].strip(), "deniedAt": now()})
    save("denials.json", denials)
    print(f"recorded: you do not have {argv[0].strip()} (shown as a confirmed gap)")
    return 0
