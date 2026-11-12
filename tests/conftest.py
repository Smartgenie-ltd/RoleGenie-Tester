"""Shared test fixtures for the BRIEF-013 production scorer swap.

`fit_stub` makes the production fit score deterministic and KEY-FREE: it pins the certified
card to a fixed dict and replaces jev_scoring.score_requirements with a pure roll-up over each
requirement's stored `match` (exercising the real rollup100 / score10 code, no Jev call). Tests
that want a fit score use `fit_stub` and give the posting >= THIN_MIN_REQUIREMENTS requirements
via the `reqs` factory. Tests that exercise the no-fallback path override score_requirements
themselves to raise jev_scoring.JevUnavailable.
"""
import pytest

import jev_scoring
import rolegenie

_CARD = {"positioning": "p", "seniority": "s", "competencies": [], "domains": [],
         "deliveryRecord": [], "certifications": [], "education": "", "knownGaps": [],
         "constraints": []}


@pytest.fixture
def fit_stub(monkeypatch):
    monkeypatch.setattr(rolegenie, "master_card", lambda cfg, **k: _CARD)

    def fake_score(card, requirements, client=None):
        labels = {f"r{i}": {"choice": (r.get("match") or "meet"), "confidence": 1.0,
                            "probabilities": {}} for i, r in enumerate(requirements)}
        r100 = jev_scoring.rollup100(requirements, labels)
        return jev_scoring.score10(r100), r100, labels

    monkeypatch.setattr(jev_scoring, "score_requirements", fake_score)
    return fake_score


@pytest.fixture
def reqs():
    def make(n=4, match="meet", importance="required"):
        return [{"id": f"r{i+1}", "text": f"req {i}", "source": f"req {i}", "kind": "tech",
                 "advertImportance": importance, "match": match} for i in range(n)]
    return make
