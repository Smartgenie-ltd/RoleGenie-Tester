"""Checkpoint trust (arch.md §11; A22; INV-RG-10; RG-S09).

Trust is replayed from the record every time, never stored as a number:
each checkpoint's measured prediction outcomes and the owner's reinstatements
(append-only trust.json), in time order. 20 correct in a row drops a
checkpoint; one miss resets the streak; a reinstatement restarts it.
Mirror of contracts/rolegenie/domain.ts replayTrust / recordPrediction.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import learning
import rolegenie

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "trust.json"
CHECKPOINTS = ("pick", "cv", "submit")
DROP_AFTER = 20
# Checkpoints that have something to measure yet. cv and submit need the
# applier (RG-S05); until then they are reported as not measured.
MEASURED = ("pick",)


def record(t: dict, correct: bool) -> dict:
    if t["state"] == "dropped":
        return t
    streak = t["streak"] + 1 if correct else 0
    return {**t, "streak": streak, "state": "dropped" if streak >= DROP_AFTER else t["state"]}


def replay(checkpoint: str, events: list[dict]) -> dict:
    t = {"checkpoint": checkpoint, "streak": 0, "state": "active"}
    for e in sorted(events, key=lambda e: e["at"]):
        t = {**t, "streak": 0, "state": "reinstated"} if e["kind"] == "reinstate" else record(t, e["correct"])
    return t


def pick_outcomes(state: dict[str, dict]) -> list[dict]:
    """Measured pick predictions, timed at the owner's first call."""
    out = []
    for e in state.values():
        o = learning.prediction_outcome(e.get("prediction"), e.get("decisions", []))
        if o != "unmeasured":
            out.append({"kind": "outcome", "at": learning.first_decision(e["decisions"])["at"],
                        "correct": o == "correct"})
    return out


def load_log(path: Path | None = None) -> list[dict]:
    path = path or LOG
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def summary(state: dict[str, dict], log: list[dict]) -> list[dict]:
    rows = []
    outcomes = {"pick": pick_outcomes(state)}
    for cp in CHECKPOINTS:
        events = outcomes.get(cp, []) + [{"kind": "reinstate", "at": r["at"]}
                                         for r in log if r.get("checkpoint") == cp]
        t = replay(cp, events)
        measured = [e for e in outcomes.get(cp, [])]
        rows.append({**t, "measured": cp in MEASURED, "outcomes": len(measured),
                     "correct": sum(e["correct"] for e in measured), "drop_after": DROP_AFTER})
    return rows


def reinstate(checkpoint: str, path: Path | None = None) -> dict:
    """Append an owner reinstatement. Always allowed (INV-RG-10)."""
    path = path or LOG
    if checkpoint not in CHECKPOINTS:
        raise ValueError(f"checkpoint must be one of {', '.join(CHECKPOINTS)}")
    entry = {"checkpoint": checkpoint, "action": "reinstate",
             "at": dt.datetime.now().isoformat(timespec="seconds")}
    with rolegenie.state_lock(path):
        log = load_log(path)
        log.append(entry)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=2), encoding="utf-8")
        tmp.replace(path)
    return entry


def brief_line(rows: list[dict]) -> str:
    parts = []
    for r in rows:
        if not r["measured"]:
            parts.append(f"{r['checkpoint']}: not measured yet")
        else:
            parts.append(f"{r['checkpoint']}: {r['streak']}/{r['drop_after']} in a row "
                         f"({r['state']}; {r['correct']} of {r['outcomes']} right overall)")
    return "Trust: " + "; ".join(parts)
