"""
E3 - the Scorer protocol, and everything that must be identical across models.

The point of this module is that a scorer cannot influence what it is asked or
how its answer is read. Anything a model could vary, and thereby confound the
comparison, lives here rather than in the adapter:

  - the prompt is rendered here and handed to the adapter as a finished string,
    so an adapter never sees the posting and cannot reformat it
  - the response schema is defined here and passed to every adapter
  - parsing is done here, identically, for every model
  - sampling and aggregation across k samples are done here

An adapter's only job is: given a prompt string and a seed, return one Sample.

This module never touches the network and never reads a board. It is pure.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------
# criteria.md section 2. Ordered, because the evaluation treats the three bands
# as an ordinal cut at two points rather than as three unrelated classes.

BANDS = ("archive", "consider", "apply")
BAND_RANK = {"archive": 0, "consider": 1, "apply": 2}

SCORE_BANDS = ((7, 10, "apply"), (4, 6, "consider"), (1, 3, "archive"))


def band_for_score(score: int) -> str:
    for lo, hi, band in SCORE_BANDS:
        if lo <= score <= hi:
            return band
    raise ValueError(f"score {score} is outside 1-10")


# ---------------------------------------------------------------------------
# The response contract
# ---------------------------------------------------------------------------
# A full JSON schema, not the string "json". Passed to the runtime so malformed
# output is structurally impossible rather than something we write recovery code
# for. SPEC.md 5.1: "A run that reports parse failures is a broken harness, not
# a model finding."

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "band": {"type": "string", "enum": list(BANDS)},
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "rationale": {"type": "string"},
    },
    "required": ["band", "score", "rationale"],
    "additionalProperties": False,
}

SCHEMA_NAME = "rolegenie_screen"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """One model call. The unit an adapter returns."""
    seed: int | None
    band: str | None
    score: int | None
    rationale: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    parse_ok: bool
    error: str = ""
    model_reported: str = ""          # what the runtime said it served
    finish_reason: str = ""           # stop | length | ...
    content_from: str = ""            # which response field the answer came from
    raw: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        return {
            "seed": self.seed, "band": self.band, "score": self.score,
            "rationale": self.rationale, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "latency_ms": self.latency_ms,
            "parse_ok": self.parse_ok, "error": self.error,
            "model_reported": self.model_reported,
            "finish_reason": self.finish_reason, "content_from": self.content_from,
        }


@dataclass
class ScoreResult:
    """The aggregate of k samples for one posting."""
    uid: str
    band: str | None
    score: int | None
    rationale: str
    samples: list[Sample]
    tokens_in: int
    tokens_out: int
    latency_ms: int
    escalated: bool
    parse_ok: bool
    n_parse_failures: int
    models_reported: list[str]
    prompt_sha256: str
    self_consistency: float

    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "band": self.band, "score": self.score,
            "rationale": self.rationale, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "latency_ms": self.latency_ms,
            "escalated": self.escalated, "parse_ok": self.parse_ok,
            "n_parse_failures": self.n_parse_failures,
            "models_reported": self.models_reported,
            "prompt_sha256": self.prompt_sha256,
            "self_consistency": self.self_consistency,
            "samples": [s.to_dict() for s in self.samples],
        }


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Scorer(Protocol):
    """Every model reaches the harness through this and nothing else.

    `sample` receives a finished prompt string. It must not modify it, must not
    add instructions of its own, and must not see the posting it came from.
    """

    name: str                        # what we asked for, e.g. "qwen3:8b"
    kind: str                        # "frontier" | "local"
    constrained: bool                # True if the runtime enforced the schema
    temperature: float
    price_per_mtok: tuple[float, float] | None   # (input, output) in USD

    def sample(self, prompt: str, seed: int | None) -> Sample: ...


# ---------------------------------------------------------------------------
# Prompt rendering - one place, so every model gets byte-identical input
# ---------------------------------------------------------------------------

RAW_BODY_CHARS = 8000        # SPEC.md 5.3: fixed, so truncation is not hidden


def render_prompt(template: str, profile: str, item: dict,
                  body_chars: int = RAW_BODY_CHARS) -> str:
    body = item.get("body", "")
    truncated = len(body) > body_chars
    return template.format(
        profile=profile.strip(),
        board=item.get("board", ""),
        title=item.get("title", ""),
        location=item.get("location", "") or "not stated",
        body=body[:body_chars] + ("\n[truncated]" if truncated else ""),
        bands=", ".join(BANDS),
        schema=json.dumps(RESPONSE_SCHEMA, indent=2),
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parsing - identical for every model
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_response(text: str) -> tuple[str | None, int | None, str, bool, str]:
    """Return (band, score, rationale, ok, error).

    Constrained decoding should make this trivial. It is written defensively
    anyway because the frontier adapter is not schema-constrained, and because a
    parse failure has to be counted rather than raised - gate check 4 is exactly
    the count of these.
    """
    if not text or not text.strip():
        return None, None, "", False, "empty response"

    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)          # last resort
        if not m:
            return None, None, "", False, "no JSON object in response"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            return None, None, "", False, f"unparseable JSON: {exc}"

    if not isinstance(obj, dict):
        return None, None, "", False, "response is not an object"

    band = obj.get("band")
    if band not in BANDS:
        return None, None, "", False, f"band {band!r} not one of {BANDS}"

    score = obj.get("score")
    if not isinstance(score, int) or not 1 <= score <= 10:
        return None, None, "", False, f"score {score!r} not an integer 1-10"

    return band, score, str(obj.get("rationale", "")), True, ""


# ---------------------------------------------------------------------------
# Sampling and aggregation - identical for every model
# ---------------------------------------------------------------------------

def aggregate(uid: str, samples: list[Sample], prompt_hash: str) -> ScoreResult:
    """Collapse k samples into one result.

    Majority band wins. Ties break toward the higher band, which is the cost
    asymmetry in criteria.md made mechanical: a missed role costs more than a
    skimmed one, so a 1-1 split between consider and apply resolves to apply.

    `escalated` is any disagreement at all among the parsed samples, not a
    failure of majority. SPEC.md 8: a split decision is exactly the case worth
    paying the frontier model for.
    """
    good = [s for s in samples if s.parse_ok]
    tokens_in = sum(s.tokens_in for s in samples)
    tokens_out = sum(s.tokens_out for s in samples)
    latency = sum(s.latency_ms for s in samples)
    models = sorted({s.model_reported for s in samples if s.model_reported})
    n_fail = len(samples) - len(good)

    if not good:
        return ScoreResult(uid=uid, band=None, score=None, rationale="",
                           samples=samples, tokens_in=tokens_in, tokens_out=tokens_out,
                           latency_ms=latency, escalated=True, parse_ok=False,
                           n_parse_failures=n_fail, models_reported=models,
                           prompt_sha256=prompt_hash, self_consistency=0.0)

    counts = Counter(s.band for s in good)
    top = max(counts.values())
    winners = [b for b, c in counts.items() if c == top]
    band = max(winners, key=lambda b: BAND_RANK[b])

    scores = [s.score for s in good if s.score is not None]
    score = int(statistics.median_low(scores)) if scores else None
    rationale = next((s.rationale for s in good if s.band == band), "")

    return ScoreResult(
        uid=uid, band=band, score=score, rationale=rationale, samples=samples,
        tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency,
        escalated=len(counts) > 1 or n_fail > 0,
        parse_ok=True, n_parse_failures=n_fail, models_reported=models,
        prompt_sha256=prompt_hash,
        self_consistency=top / len(good),
    )


def score_item(scorer: Scorer, uid: str, prompt: str, k: int,
               seeds: list[int] | None = None) -> ScoreResult:
    """Run one posting through one scorer k times and aggregate.

    Every model goes through this function. No adapter gets to decide how many
    samples it takes or how they are combined.
    """
    if seeds is None:
        seeds = list(range(k))
    samples = [scorer.sample(prompt, seeds[i] if i < len(seeds) else i)
               for i in range(k)]
    return aggregate(uid, samples, sha256(prompt))


def sampling_is_degenerate(temperature: float, k: int) -> bool:
    """True when k samples cannot possibly differ.

    Greedy decoding is deterministic, so at temperature 0 every sample is
    identical, self-consistency is 1.0 by construction and the escalation path
    can never fire. SPEC.md 9.1 resolves this to temperature 0.8 with a pinned
    seed per sample. The harness does not overrule the caller, it records the
    condition so a 1.0 consistency figure is never mistaken for evidence.
    """
    return temperature == 0 and k > 1
