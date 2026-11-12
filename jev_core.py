"""Jev (System One) product core (CR-rg-jev-judge-move): the pinned model, the match criteria and the helpers the
product needs to score requirements. The product imports only this module, so a clone without the owner's
evaluation folder scores identically. Owner-tuned track criteria live with the evaluation scripts, not here.

Key from the gitignored .env at the repo root (or the environment) only; never logged, committed or echoed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "jev-1.13.0"  # PINNED (skill): do not use the moving jev-latest alias
MATCH_OPTS = ["meet", "partial", "gap", "unknown"]

MATCH_CRITERIA = {
    "meet": {"what": "`candidate` gives direct, comparable evidence that clearly satisfies this requirement.",
             "not_for": "related-but-lesser evidence (partial); an explicitly stated absence (gap); or silence (unknown)."},
    "partial": {"what": "`candidate` shows related or lesser-scope evidence - adjacent capability, smaller scale, or older - short of direct comparable evidence.",
                "not_for": "direct comparable evidence (meet); a stated absence (gap); or silence (unknown)."},
    "gap": {"what": "`candidate` explicitly states the absence of this, or a stated limitation in `candidate.knownGaps`/`candidate.constraints` covers it.",
            "not_for": "any positive evidence, direct or related (meet/partial); or mere silence with no stated limitation (unknown)."},
    "unknown": {"what": "`candidate` is simply silent on this - no evidence and no stated limitation either way.",
                "not_for": "any stated evidence (meet/partial); or any stated absence (gap)."},
}


def load_key() -> bool:
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                os.environ["TYPESAFE_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def sanitize(text: str) -> str:
    t = " ".join((text or "").split())
    low = t.lower()
    for m in ("ignore previous", "ignore the above", "disregard", "system prompt", "you are now", "new instructions"):
        if m in low:
            return "[requirement text omitted: instruction-shaped]"
    return t[:600]


def est_tokens(obj) -> int:
    """Rough upper-bound token estimate: chars/4, +40% for the documented dense-JSON undercount."""
    return int(len(json.dumps(obj)) / 4 * 1.4)


def judge(client, ts, state, questions, label):
    assert est_tokens(state) + est_tokens({k: q.model_dump() for k, q in questions.items()}) < 60000, \
        f"{label}: request near 64k budget"
    resp = client.system_one(state=state, questions=questions, model=MODEL)
    assert resp.model == MODEL, f"model drift: requested {MODEL}, answered {resp.model}"
    return resp
