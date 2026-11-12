"""Production fit scorer (BRIEF-013, CR-rg-score-rollup): Jev judges each decomposed
requirement's match against the certified card, and code rolls it up with FIXED declared
weights into a 0-100 fit score and a 0-10 board score.

Single-sourced, NOT forked: the skill-conformant match criteria, the pinned model string,
sanitize() and the token estimator come from jev_core.py (BRIEF-008/012/033). The declared
weights mirror contracts/rolegenie/domain.ts (MATCH_WEIGHTS / IMPORTANCE_WEIGHTS /
THIN_MIN_REQUIREMENTS / SCORE_SCALE).

No fallback (owner's ruling): if the Jev call fails or TYPESAFE_API_KEY is absent, this raises
JevUnavailable and the caller leaves the posting unscored - the score NEVER silently falls back
to an LLM guess.
"""
from __future__ import annotations

import jev_core as jj  # single source: MATCH_CRITERIA, MODEL, sanitize, est_tokens, load_key

# --- declared weights (mirror of domain.ts; fixed, never fitted to owner decisions) ---
MATCH_WEIGHTS = {"meet": 1.0, "partial": 0.5, "gap": 0.0, "unknown": 0.0}
IMPORTANCE_WEIGHTS = {"required": 1.0, "preferred": 0.5, "contextual": 0.25}
SCORE_SCALE = 100
THIN_MIN_REQUIREMENTS = 4

MODEL = jj.MODEL


class JevUnavailable(Exception):
    """Jev could not score (no key, or the call failed). The caller must NOT fall back."""


def rollup100(requirements: list[dict], labels: dict[str, dict]) -> float:
    """100 * Σ(importance_w · match_w) / Σ(importance_w) over the Jev match labels.
    `labels` maps the per-requirement id 'r{i}' to {'choice': ...}. Mirrors
    the evaluation roll-up coverage() with the declared production weights."""
    num = den = 0.0
    for i, r in enumerate(requirements):
        iw = IMPORTANCE_WEIGHTS.get(r.get("advertImportance"), 1.0)
        mw = MATCH_WEIGHTS.get(labels.get(f"r{i}", {}).get("choice"), 0.0)
        num += iw * mw
        den += iw
    return SCORE_SCALE * num / den if den else 0.0


def score10(rollup: float) -> int:
    """Board score 0-10 = round(rollup100 / 10); keeps bands/triage/tests meaning."""
    return round(rollup / 10)


def match_requirements(client, ts, card: dict, requirements: list[dict]) -> dict[str, dict]:
    """One Jev call for a posting: a Choice {meet,partial,gap,unknown} per requirement against
    `{"candidate": card}` as nested-JSON state, per the /jev skill. Returns
    {'r{i}': {'choice','confidence','probabilities'}}. Raises JevUnavailable on any failure."""
    if not requirements:
        return {}
    qs = {f"r{i}": ts.Choice(
            instructions={"question": "Which option best describes the evidence in `candidate` "
                                      "(competencies, deliveryRecord, domains, certifications, "
                                      "education) for this job requirement.",
                          "requirement": jj.sanitize(r.get("text", ""))},
            criteria=jj.MATCH_CRITERIA)
          for i, r in enumerate(requirements)}
    state = {"candidate": card}
    # skill budgets: <=50 questions/call, state+longest question < 32k, whole request < 64k
    assert len(qs) <= 50, f"{len(qs)} questions > 50-per-call limit"
    st = jj.est_tokens(state)
    qtoks = {k: jj.est_tokens(q.model_dump()) for k, q in qs.items()}
    assert st + max(qtoks.values(), default=0) < 32000, "state + longest question near 32k"
    assert st + sum(qtoks.values()) < 64000, "request near 64k"
    try:
        resp = client.system_one(state=state, questions=qs, model=MODEL)
    except Exception as exc:  # noqa: BLE001 - any SDK/transport error is a no-score, not a fallback
        raise JevUnavailable(str(exc)[:200]) from exc
    if resp.model != MODEL:
        raise JevUnavailable(f"model drift: requested {MODEL}, answered {resp.model}")
    return {k: {"choice": a.choice, "confidence": round(a.confidence, 4),
                "probabilities": {o: round(v, 4) for o, v in a.probabilities.items()}}
            for k, a in resp.answers.items()}


def score_requirements(card: dict, requirements: list[dict], client=None):
    """Score one posting's requirements with Jev. Returns (score10, rollup100, labels).
    Raises JevUnavailable if the key is absent or the call fails (no fallback). A caller may
    pass a live client to reuse one across a batch; otherwise one is opened and closed here."""
    import typesafe_sdk as ts
    if not jj.load_key():
        raise JevUnavailable("TYPESAFE_API_KEY absent")
    own = client is None
    if own:
        client = ts.TypeSafeClient(model=MODEL)
    try:
        labels = match_requirements(client, ts, card, requirements)
    finally:
        if own:
            client.close()
    r100 = rollup100(requirements, labels)
    return score10(r100), r100, labels
