"""Slice 1 assessment (BRIEF-022, CR-rg-slice1-assessment; contract: contracts/rolegenie/assessment.ts).

One job ad + the user's APPROVED master -> an evidence-linked assessment: score100 (the production roll-up, weights
unchanged) with per-requirement contributions, and four separate lists - unmet hard requirements (printed first,
whatever the score), confirmed gaps, missing evidence, uncertain requirements.

    rolegenie.py assess [--company NAME] [--url URL] [AD_FILE | -]   ("-" reads the ad from stdin)

Reused, not rebuilt: rolegenie.segment_advert / _segments_within_budget (every requirement is a verbatim ad segment),
run_model + parse_model_json (via onboard.call_model, Claude explicitly), jev_scoring.score_requirements (pinned Jev,
JevUnavailable, no fallback) and the declared weights. A new decomposition prompt is needed because DECOMPOSE_PROMPT
carries a candidate profile and a match field and has no `uncertain` flag; changing it would change the owner's
nightly pipeline. Self-ratings are never read here.

Slice 1b (BRIEF-024, CR-rg-slice1b-evidence-links): after Jev labels the requirements, ONE Claude call cites, for each
meet/partial requirement, the approved master fact ids behind it (AC2b). Code keeps only cited ids that exist in the
kept, approved master (assessment.ts evidenceLinks); a match left with none is listed in `unlinkedMatches` and printed
as "met - evidence not linked", never dropped, and the score is untouched (R2). The ad's title line is not a
requirement; an unreadable ad is a named AdUnreadable.

From a job link (BRIEF-029, CR-rg-new-user-journey; contract assessment.ts AdSource, adUrlProblems, boardUid): with
`--url` and no AD_FILE the ad text is fetched with the scan's own fetcher (hydrate_fetch.fetch_advert) and judged by
the scan's clean-fetch rule (rolegenie.hydration_problems); a failed or unclean fetch is AdUnreadable and nothing is
written. With an AD_FILE the link is only recorded. `--company` is recorded as typed; nothing is guessed from the ad.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys

import jev_scoring
import onboard
from onboard import MasterNotApproved, ModelUnavailable, fail, load, save, span_verified

_real_score_requirements = jev_scoring.score_requirements
IMPORTANCE_LEVEL = {"required": "hard", "preferred": "nice", "contextual": "passing"}
MAX_SEGMENTS = 120
JEV_MAX_QUESTIONS = 50
FACT_FIELD = {"skill": "competencies", "other": "competencies", "role": "deliveryRecord",
              "achievement": "deliveryRecord", "domain": "domains", "certification": "certifications",
              "education": "education"}


class AdUnreadable(Exception):
    """The ad file cannot be read or decoded as text (missing, unsupported type, not UTF-8, binary). Nothing written."""


# A first ad line with none of these words is a title or header, never a requirement (BRIEF-024 AC-title).
REQUIREMENT_VERB = re.compile(r"\b(must|required|requires?|requirement|need|needs|needed|essential|experience|"
                              r"years?|knowledge|ability|able to|should|proficien\w*|familiar\w*|hold|have)\b",
                              re.IGNORECASE)


def is_title_line(span: str, ad_text: str) -> bool:
    """True when `span` is the ad's first non-empty line (bullet-stripped, whitespace-normalised) and states no need."""
    import rolegenie
    first = next((ln for ln in ad_text.splitlines() if ln.strip()), "")
    first = rolegenie._BULLET.sub("", first.strip()).strip()
    return onboard.norm(span) == onboard.norm(first) and not REQUIREMENT_VERB.search(span)


# BRIEF-065 (CR-rg-ad-noise-not-requirements; mirror of assessment.ts adConditionReason): an ad line a resume cannot
# evidence is an ad condition - visible in `adConditions`, never scored, never an unmet hard requirement (F3).
_QUESTION = re.compile(r"\?[\s*]*$|^\W*(are|do|will|would|can|could|have|is|what|when|how)\s+(you|your)\b", re.IGNORECASE)
_FORM_TOPIC = re.compile(r"authori[sz]|right to work|sponsor|visa|work permit|in[- ]person|on[- ]?site|in the office|"
                         r"relocat|commut|notice period|start date|salary|compensation|pay expectation", re.IGNORECASE)
# BRIEF-069 F1 ruling: a labelled application/screening question, or a "why do you want / why are you interested"
# question, is a form question too (a motivation is not a capability the master can evidence, R2).
_FORM_LABEL = re.compile(r"^\W*(application|screening)\s+questions?\s*\d*\s*[:.\-–—]\s*\S", re.IGNORECASE)
_MOTIVATION = re.compile(r"\bwhy\s+(do|would)\s+you\s+want\b|\bwhy\s+are\s+you\s+interested\b", re.IGNORECASE)


_CAPABILITY_WORD = re.compile(
    r"\b(experience\w*|years?|built|build\w*|deliver\w*|design\w*|led|lead\w*|used|using|worked with|work(?:ing)? with|"
    r"develop\w*|implement\w*|architect\w*|maintain\w*|manag\w*|migrat\w*|deploy\w*|engineer\w*|program\w*|automat\w*|"
    r"knowledge|proficien\w*|familiar\w*|expert\w*|skill\w*|background in|track record)\b", re.IGNORECASE)
_NAMED_TECH = re.compile(r"\b[A-Z][A-Z0-9]{1,5}s?\b|\b\w*[a-z][A-Z]\w*\b|\b[A-Za-z]+\d+[A-Za-z]*\b")
_NOT_TECH = {"UK", "US", "USA", "EU", "EEA", "UAE", "NYC", "SF", "LA", "GB", "EMEA", "APAC", "UTC", "GMT", "CET", "EST",
             "PST"}


def has_capability_content(span: str) -> bool:
    """Mirror of assessment.ts hasCapabilityContent: a capability word or a named technology (rejection R1/R2)."""
    return bool(_CAPABILITY_WORD.search(span)) or any(t not in _NOT_TECH for t in _NAMED_TECH.findall(span))


def ad_condition_reason(span: str, kind: str) -> str | None:
    """'form-question', 'constraint' or 'location' when the line is an ad condition, else None (scored). A line with
    capability content is always scored."""
    if has_capability_content(span):
        return None
    if _FORM_LABEL.search(span) or (_QUESTION.search(span.strip()) and (_FORM_TOPIC.search(span)
                                                                     or _MOTIVATION.search(span))):
        return "form-question"
    if kind == "right-to-work":
        return "constraint"
    if kind in ("location", "onsite-pattern"):
        return "constraint" if REQUIREMENT_VERB.search(span) else "location"
    return None


def condition_lines(conditions: list[dict]) -> list[str]:
    """Mirror of assessment.ts conditionLines: "Condition: <text>" for each stated constraint, for `uncertain` (R3)."""
    return [f"Condition: {c['text']}" for c in conditions if c["reason"] == "constraint"]


class JevBudgetExceeded(Exception):
    """The Jev request would exceed the skill budgets (50 questions, 32k/64k tokens). Nothing is truncated."""


ASSESS_DECOMPOSE_PROMPT = """AD DECOMPOSITION (RoleGenie slice 1)

You are classifying lines taken verbatim from one job advert. Return one object for EACH line that states something
a candidate is judged on: a qualification, skill, experience, eligibility condition (location, right-to-work,
clearance, onsite/travel pattern) or a job duty. Do NOT return a line that is not an ask: recruitment or announcement
sentences ("We are seeking..."), company or team descriptions, benefits, equal-opportunity boilerplate, section
headings, bullet-list lead-ins, metadata or keyword lines. The advert's title or header line (a job title, often with the company, place or work
pattern, e.g. "Data Engineer - Acme Ltd (Leeds, hybrid)") is never a requirement: do not return it.

For each line you return:
- i: the line number exactly as shown (never invent or renumber lines)
- kind: one of tech, competency, soft, location, onsite-pattern, clearance, right-to-work, qualification, domain
- advert_importance: read what a SECTION DOES, not how its heading is worded. First decide whether the advert HAS a
  hard requirements section (headed "Requirements", "What we need to see", "Minimum qualifications" or similar, or
  worded with must / must-have / minimum).
    * "required" = a hard requirement: an item in that hard requirements section; OR, when the advert has NO hard
      requirements section and its only qualifications sit under a softened heading ("Ideally...", "You might thrive
      if..."), the items of that section (the advert's ONLY qualifications section IS its requirements section).
      Always "required": a stated location, onsite, clearance or right-to-work condition, and any line whose own text
      says "required", "must" or "essential", wherever it sits.
    * "preferred" = a nice-to-have: an item under a softened heading ("nice to have", "ideally", "a plus", "bonus",
      "desirable") WHEN the advert also has a hard requirements section.
    * "contextual" = mentioned in passing: a duty that describes the work the person will do ("What you'll do",
      "In this role you will"), or a tool or topic merely mentioned. Describing the work is not requiring prior
      experience in it. A duty line that itself states a need of the candidate ("you must travel weekly") is classed
      by that need instead.
  When one line offers alternatives ("A or B"), it is one requirement at the importance the advert gives the line.
- uncertain: true when the clause is hedged or ambiguous, so it is unclear what is really asked or how much it matters
  ("may be useful", "might", "could be useful", "depending on", "some exposure to", "ideally some", "possibly",
  "familiarity with something like", an unclear scope or level); false when the ask is clear. An accepted alternative
  ("X or equivalent", "X or similar", "X or a related tool") is a flexibility, not a hedge: the ask is still clear,
  so it is NOT uncertain on that account.

Lines:
{lines}

Respond with JSON only, no markdown fences, no preamble:
{{"requirements": [{{"i": <int>, "kind": "<kind>", "advert_importance": "<required|preferred|contextual>", "uncertain": <true|false>}}, ...]}}
"""


def decompose_ad(ad_text: str, cfg: dict) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """(requirements, unassessed segments, problems, ad conditions). Each requirement and condition is a verified
    verbatim ad segment; a condition (ad_condition_reason) is kept apart and never scored."""
    import rolegenie
    segs = rolegenie.segment_advert(ad_text, max_segments=10 ** 6)
    sent = rolegenie._segments_within_budget(segs[:MAX_SEGMENTS])
    unassessed = segs[len(sent):]
    if not sent:
        return [], unassessed, ["the ad has no text lines"], []
    parsed = onboard.call_model(
        ASSESS_DECOMPOSE_PROMPT.format(lines="\n".join(f"{i}. {s}" for i, s in enumerate(sent))), cfg)
    reqs, problems, seen, conditions = [], [], set(), []
    for v in parsed.get("requirements") or []:
        i = v.get("i") if isinstance(v, dict) else None
        if type(i) is not int or not 0 <= i < len(sent) or i in seen:
            problems.append(f"model line reference not usable: {str(v)[:120]}")
            continue
        if (v.get("kind") not in rolegenie.REQUIREMENT_KINDS or v.get("advert_importance") not in IMPORTANCE_LEVEL
                or not isinstance(v.get("uncertain"), bool)):
            problems.append(f"line {i} classification invalid: {str(v)[:120]}")
            continue
        if not span_verified(sent[i], ad_text):
            problems.append(f"line {i} is not verbatim in the ad")
            continue
        if is_title_line(sent[i], ad_text):
            problems.append(f"line {i} is the ad's title line, not a requirement: {sent[i][:120]}")
            continue
        seen.add(i)
        reason = ad_condition_reason(sent[i], v["kind"])
        if reason:
            conditions.append({"text": sent[i], "span": sent[i], "kind": v["kind"], "reason": reason})
            continue
        reqs.append({"text": sent[i], "span": sent[i], "kind": v["kind"],
                     "advertImportance": v["advert_importance"],
                     "importance": IMPORTANCE_LEVEL[v["advert_importance"]], "uncertain": v["uncertain"]})
    # BRIEF-069: a form question is listed whatever the model returned for it (dropped, garbled, unusable reference);
    # judged without a model kind, never scored, no extra call. A line the model got right is already in `seen`.
    for i, s in enumerate(sent):
        if (i not in seen and s not in {c["text"] for c in conditions} and span_verified(s, ad_text)
                and not is_title_line(s, ad_text) and ad_condition_reason(s, "") == "form-question"):
            conditions.append({"text": s, "span": s, "kind": None, "reason": "form-question"})
    reqs.sort(key=lambda r: sent.index(r["span"]))
    conditions.sort(key=lambda c: sent.index(c["span"]))
    for n, r in enumerate(reqs, 1):
        r["id"] = f"r{n}"
    return reqs, unassessed, problems, conditions


def kept_facts(master: dict) -> list[dict]:
    """The approved master facts in use: those the user set aside when resolving a contradiction are left out."""
    dropped = {fid for c in master["contradictions"] if c.get("status") == "resolved"
               for fid in c["factIds"] if fid != c.get("keep")}
    return [f for f in master["facts"] if f["id"] not in dropped]


def jev_card(master: dict, denials: list[dict]) -> dict:
    """Jev state from APPROVED master facts only (AC2a), on the card fields match_requirements already asks about.
    Facts the user set aside when resolving a contradiction are left out. Denials are stated absences (AC4)."""
    card = {k: [] for k in ("competencies", "deliveryRecord", "domains", "certifications", "education")}
    for f in kept_facts(master):
        card[FACT_FIELD.get(f["category"], "competencies")].append(f["text"])
    card["knownGaps"] = [f"No {d['capability']} (stated by the candidate)" for d in denials]
    card["constraints"] = []
    return card


def check_jev_budget(card: dict, requirements: list[dict]) -> None:
    """Pre-check the /jev skill budgets before any call (kill criterion 2): never truncate to fit."""
    jj = jev_scoring.jj
    if len(requirements) > JEV_MAX_QUESTIONS:
        raise JevBudgetExceeded(f"{len(requirements)} requirements > {JEV_MAX_QUESTIONS} questions per Jev call")
    st = jj.est_tokens({"candidate": card})
    q = [jj.est_tokens({"instructions": {"question": "x" * 200, "requirement": jj.sanitize(r.get("text", ""))},
                        "criteria": jj.MATCH_CRITERIA}) for r in requirements]
    if st + max(q, default=0) >= 32000 or st + sum(q) >= 64000:
        raise JevBudgetExceeded(f"master state ~{st} tokens is over the Jev request budget (32k/64k)")


def assessment_of(requirements: list[dict], labels: dict[str, str]) -> dict:
    """Mirror of assessment.ts assessmentOf: requirements and Jev choices only, no user rating (AC7)."""
    iw, mw = jev_scoring.IMPORTANCE_WEIGHTS, jev_scoring.MATCH_WEIGHTS
    den = sum(iw[r["advertImportance"]] for r in requirements)
    contributions = []
    for r in requirements:
        choice = labels.get(r["id"], "unknown")
        contributions.append({"id": r["id"], "importance": IMPORTANCE_LEVEL[r["advertImportance"]], "choice": choice,
                              "contribution": 100 * iw[r["advertImportance"]] * mw[choice] / den if den else 0.0})
    return {"score100": sum(c["contribution"] for c in contributions), "contributions": contributions,
            "unmetHard": [c["id"] for c in contributions if c["importance"] == "hard" and c["choice"] != "meet"],
            "confirmedGaps": [c["id"] for c in contributions if c["choice"] == "gap"],
            "missingEvidence": [c["id"] for c in contributions if c["choice"] == "unknown"],
            "uncertain": [r["id"] for r in requirements if r["uncertain"]]}


LINK_PROMPT = """EVIDENCE LINKING (RoleGenie slice 1b)

A candidate's approved facts are listed below, each with an id, and then the job requirements an assessor has already
judged the candidate to meet or partly meet. For EACH requirement, cite the id(s) of the fact(s) that support that
judgement: the facts a reader should check to see why it is met. Cite only ids from the list, never invent one. Cite
the most direct facts first; cite none (an empty list) if no listed fact supports the requirement. Do not judge
whether the requirement is met: that is already decided.

Facts:
{facts}

Requirements:
{requirements}

Respond with JSON only, no markdown fences, no preamble:
{{"links": [{{"id": "<requirement id>", "facts": ["<fact id>", ...]}}, ...]}}
"""


def link_evidence(facts: list[dict], reqs: list[dict], cfg: dict) -> tuple[dict[str, list[str]], list[str]]:
    """(citations, problems): one Claude call citing fact ids for the given (meet/partial) requirements. The answer
    is untrusted: malformed items are reported, and evidence_links verifies every id against the approved master."""
    if not reqs:
        return {}, []
    parsed = onboard.call_model(LINK_PROMPT.format(
        facts="\n".join(f"{f['id']}: {f['text']}" for f in facts),
        requirements="\n".join(f"{r['id']}: {r['text']}" for r in reqs)), cfg)
    asked, citations, problems = {r["id"] for r in reqs}, {}, []
    for v in parsed.get("links") or [] if isinstance(parsed, dict) else []:
        ok = (isinstance(v, dict) and v.get("id") in asked and isinstance(v.get("facts"), list)
              and all(isinstance(x, str) for x in v["facts"]))
        if not ok:
            problems.append(f"link item not usable: {str(v)[:120]}")
            continue
        citations.setdefault(v["id"], []).extend(v["facts"])
    return citations, problems


def evidence_links(contributions: list[dict], citations: dict[str, list[str]],
                   approved_fact_ids: list[str]) -> dict:
    """Mirror of assessment.ts evidenceLinks: verified ids only; a meet/partial with none is an unlinked match."""
    ok = set(approved_fact_ids)
    evidence, unlinked = {}, []
    for c in contributions:
        if c["choice"] not in ("meet", "partial"):
            continue
        ids = list(dict.fromkeys(f for f in citations.get(c["id"], []) if f in ok))
        if ids:
            evidence[c["id"]] = ids
        else:
            unlinked.append(c["id"])
    return {"evidence": evidence, "unlinkedMatches": unlinked}


def approved_master() -> dict:
    """The approved master, or a named MasterNotApproved saying why not (no fallback to any other profile)."""
    m = load("master.json", None)
    if m is None:
        raise MasterNotApproved("no master yet: run rolegenie.py onboard RESUME_FILE... first")
    probs = onboard.approval_problems(onboard.master_sha(m), m["contradictions"], load("approval.json", None))
    if probs:
        raise MasterNotApproved("; ".join(probs) + " (rolegenie.py master show / master approve)")
    return m


def assess(ad_text: str, ad_name: str, cfg: dict, company: str | None = None,
           url: str | None = None, posting_uid: str | None = None) -> tuple[dict, object]:
    m = approved_master()
    reqs, unassessed, problems, conditions = decompose_ad(ad_text, cfg)
    if not reqs:
        extra = [f"{len(conditions)} line(s) were ad conditions, not requirements: "
                 + "; ".join(c["text"] for c in conditions)] if conditions else []
        raise ModelUnavailable("the decomposition returned no usable requirements: "
                               + "; ".join(extra + problems)[:300])
    card = jev_card(m, load("denials.json", []))
    check_jev_budget(card, reqs)
    _, _, labels = jev_scoring.score_requirements(card, [{"text": r["text"], "advertImportance": r["advertImportance"]}
                                                         for r in reqs])
    choices = {r["id"]: (labels.get(f"r{i}") or {}).get("choice", "unknown") for i, r in enumerate(reqs)}
    a = assessment_of(reqs, choices)
    a["uncertain"] += condition_lines(conditions)   # stated constraints reach the board's Uncertain list (R3)
    for i, (r, c) in enumerate(zip(reqs, a["contributions"])):
        r["choice"], r["contribution"] = c["choice"], c["contribution"]
        r["confidence"] = (labels.get(f"r{i}") or {}).get("confidence")
    facts = kept_facts(m)
    citations, link_problems = link_evidence(facts, [r for r in reqs if r["choice"] in ("meet", "partial")], cfg)
    links = evidence_links(a["contributions"], citations, [f["id"] for f in facts])
    by_id = {f["id"]: f for f in facts}
    for r in reqs:
        r["evidence"] = [{"factId": fid, "text": by_id[fid]["text"], "sources": by_id[fid]["sources"]}
                         for fid in links["evidence"].get(r["id"], [])]
    ad_sha = hashlib.sha256(ad_text.encode("utf-8")).hexdigest()[:16]
    out = {"ad": ad_name, "adSha": ad_sha, "masterSha": onboard.master_sha(m),
           "assessedAt": dt.datetime.now().isoformat(timespec="seconds"), "jevModel": jev_scoring.MODEL,
           "score100": a["score100"], "requirements": reqs, "adConditions": conditions,
           **{k: a[k] for k in ("unmetHard", "confirmedGaps", "missingEvidence", "uncertain")},
           **links, "unassessedSegments": unassessed, "decompositionProblems": problems,
           "linkProblems": link_problems,
           **{k: v.strip() for k, v in (("company", company), ("url", url)) if v and v.strip()},
           **({"postingUid": posting_uid} if posting_uid else {})}
    path = save(f"assessments/assessment-{ad_sha}.json", out)
    return out, path


def render(a: dict) -> str:
    by = {r["id"]: r for r in a["requirements"]}

    def line(rid):
        if rid not in by:                               # a "Condition: <text>" line (condition_lines)
            return f"  {rid}"
        r = by[rid]
        return f"  {rid} [{r['importance']}{', uncertain' if r['uncertain'] else ''}] {r['text']}  ({r['choice']})"
    out = ["UNMET HARD REQUIREMENTS (shown whatever the score):"]
    out += [line(i) for i in a["unmetHard"]] or ["  none"]
    if a["unassessedSegments"]:
        out.append(f"NOT ASSESSED ({len(a['unassessedSegments'])} ad lines beyond the model budget; read them yourself):")
        out += [f"  {s}" for s in a["unassessedSegments"]]
    out.append(f"\nScore {a['score100']:.1f} / 100 (evidence against requirements; your ratings never change it)")
    out.append("Per requirement (contribution to the score; for a match, the resume line behind it):")
    for r in a["requirements"]:
        out.append(f"  {r['id']} [{r['importance']}] {r['choice']:<8} {r['contribution']:5.1f}  {r['text']}")
        for e in r["evidence"]:
            files = ", ".join(dict.fromkeys(s["file"] for s in e["sources"])) or "confirmed by you"
            out.append(f"      evidence: {e['factId']} \"{e['text']}\" ({files})")
        if r["id"] in a["unlinkedMatches"]:
            out.append("      met - evidence not linked (check this one yourself)")
    out.append("\nCONFIRMED GAPS (you or your resumes state you lack it):")
    out += [line(i) for i in a["confirmedGaps"]] or ["  none"]
    out.append("MISSING EVIDENCE (your master says nothing either way):")
    out += [line(i) for i in a["missingEvidence"]] or ["  none"]
    out.append("UNCERTAIN (the ad is hedged or unclear here):")
    out += [line(i) for i in a["uncertain"]] or ["  none"]
    out.append("MATCHED BUT EVIDENCE NOT LINKED (judged met, but no fact of yours was cited; check it yourself):")
    out += [line(i) for i in a["unlinkedMatches"]] or ["  none"]
    if a.get("adConditions"):
        out.append("IN THE AD, NOT SCORED (location, application-form questions, stated conditions; check them yourself):")
        out += [f"  [{c['reason']}] {c['text']}" for c in a["adConditions"]]
    if a["decompositionProblems"]:
        out.append(problem_summary(a["decompositionProblems"]))
    return "\n".join(out)


_PROBLEM_KINDS = (("model line reference not usable", "unusable line reference"),
                  ("classification invalid", "invalid classification"),
                  ("is not verbatim in the ad", "not verbatim in the ad"), ("title line", "the ad's title line"))


def problem_summary(problems: list[str]) -> str:
    """BRIEF-069: the printed line for the model answers decompose_ad did not use: the count, why, and the JSON key."""
    counts: dict[str, int] = {}
    for p in problems:
        kind = next((label for key, label in _PROBLEM_KINDS if key in p), "other")
        counts[kind] = counts.get(kind, 0) + 1
    why = ", ".join(f"{n} {k}" for k, n in counts.items())
    return f'Model answers not used: {len(problems)} ({why}). Details: "decompositionProblems" in the assessment JSON.'


def read_ad(arg: str) -> tuple[str, str]:
    """(text, name) of the ad file, or "-" for stdin. Anything that is not readable text is a named AdUnreadable."""
    from pathlib import Path
    try:
        if arg == "-":
            text, name = sys.stdin.buffer.read().decode("utf-8-sig"), "stdin"
        else:
            text, name = onboard.read_resume(Path(arg)), Path(arg).name
    except UnicodeDecodeError as exc:
        raise AdUnreadable(f"stdin: not UTF-8 text ({exc.reason})") from exc
    except onboard.ResumeUnreadable as exc:
        raise AdUnreadable(str(exc)) from exc
    if "\x00" in text:
        raise AdUnreadable(f"{name}: binary content (NUL bytes), not a text ad")
    if not text.strip():
        raise AdUnreadable(f"{name}: no text")
    return text, name


AD_URL_PROBLEM = "the ad link must start with http:// or https://"
UNREADABLE_FIX = " - save the ad text to a file and pass it instead: rolegenie.py assess --url URL AD_FILE"


def ad_url_problems(url: str) -> list[str]:
    """Mirror of assessment.ts adUrlProblems: only an http(s) link is a job link."""
    return [] if url.strip().lower().startswith(("http://", "https://")) else [AD_URL_PROBLEM]


def normalise_ad_url(url: str) -> str:
    """Mirror of assessment.ts normaliseAdUrl (the Adzuna rule is hydrate_fetch.details_url's)."""
    import hydrate_fetch
    return hydrate_fetch.details_url(url) or url.strip()


def board_uid(a: dict) -> str:
    """Mirror of assessment.ts boardUid: the normalised link when there is one, else the ad's sha."""
    return f"url:{normalise_ad_url(a['url'])}" if a.get("url") else a["adSha"]


def read_ad_url(url: str) -> tuple[str, str]:
    """(text, name) of the ad at `url`, fetched like the scan does; anything but a clean fetch is AdUnreadable."""
    import hydrate_fetch
    import rolegenie
    url = url.strip()
    try:
        text, status = hydrate_fetch.fetch_advert(normalise_ad_url(url))
    except Exception as exc:  # noqa: BLE001 - any fetch failure is the same named error for the user
        first = (str(exc).strip().splitlines() or [""])[0][:200]   # e.g. Playwright's multi-line banner
        raise AdUnreadable(f"{url}: could not read the page ({type(exc).__name__}: {first})" + UNREADABLE_FIX) from exc
    problems = rolegenie.hydration_problems(text, status)
    if problems:
        raise AdUnreadable(f"{url}: {'; '.join(problems)}" + UNREADABLE_FIX)
    return text, url


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rolegenie.py assess", allow_abbrev=False,
                                description="Assess one job ad against your approved master CV.")
    p.add_argument("--company", help="the employer's name, recorded as you type it (shown on the board)")
    p.add_argument("--url", help="the job's link: the ad is read from it when no AD_FILE is given; "
                                 "with an AD_FILE it is only recorded (shown on the board)")
    p.add_argument("--pending", action="store_true",
                   help="assess the scanned postings still waiting (blocked, failed, or on a changed master)")
    p.add_argument("--limit", type=int, default=30, help="with --pending: at most this many postings (newest first)")
    p.add_argument("ad_file", nargs="?", metavar="AD_FILE", help='the ad saved as a text file, or "-" for stdin')
    return p


def cmd_assess(argv: list[str], cfg: dict) -> int:
    onboard._out()
    p = _parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:          # -h prints usage (0); a bad option prints usage (2)
        return exc.code if isinstance(exc.code, int) else 2
    if args.pending:
        import rolegenie
        counts = rolegenie.assess_pending(cfg, limit=args.limit)
        print("Pending postings assessed: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none due"))
        return 0
    if args.ad_file is None and args.url is None:
        p.print_usage(sys.stderr)
        return 2
    if args.url is not None and ad_url_problems(args.url):
        p.print_usage(sys.stderr)
        print(f"rolegenie.py assess: {AD_URL_PROBLEM}", file=sys.stderr)
        return 2
    try:
        ad_text, ad_name = read_ad(args.ad_file) if args.ad_file is not None else read_ad_url(args.url)
        a, path = assess(ad_text, ad_name, cfg, company=args.company, url=args.url)
    except (AdUnreadable, MasterNotApproved, ModelUnavailable, JevBudgetExceeded, jev_scoring.JevUnavailable) as exc:
        print("No assessment written.", file=sys.stderr)
        return fail(exc)
    print(render(a))
    print(f"\nAssessment written: {path}")
    return 0
