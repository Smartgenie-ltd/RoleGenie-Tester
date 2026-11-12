"""ATS parseability checker (CR-rg-ats-check).

A resume must parse cleanly before any human sees it - the first gate on the only
project aim, obtaining interviews. This runs a documented, editable ruleset
(ats_rules.yaml) against a CV's .docx and .pdf and reports, per rule, whether it
passes. Deterministic code, no runtime model (ENG-032).

The verdict/heading/date helpers mirror contracts/rolegenie/domain.ts exactly; the
file-reading primitives (tables, images, columns, PDF text) live only here. The
rules themselves are DATA in ats_rules.yaml so a parallel ATS-alignment effort can
tune them without changing this engine.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "ats_rules.yaml"

# --- pure mirrors of contracts/rolegenie/domain.ts (keep in lockstep) ---------

ATS_SEVERITIES = ("blocker", "warning")
ATS_VERDICTS = ("clean", "warnings", "blocked")
ATS_CANONICAL_SECTIONS = (
    "Summary", "Experience", "Education", "Skills", "Certifications", "Projects", "Languages",
)


def ats_verdict(findings: list[dict]) -> str:
    """Mirror of domain.ts atsVerdict: a failed blocker blocks, else a failed
    warning warns, else clean."""
    if any((not f["passed"]) and f["severity"] == "blocker" for f in findings):
        return "blocked"
    if any((not f["passed"]) and f["severity"] == "warning" for f in findings):
        return "warnings"
    return "clean"


def ats_sendable(findings: list[dict]) -> bool:
    """Mirror of domain.ts atsSendable: may send unless a blocker is open."""
    return ats_verdict(findings) != "blocked"


_HEAD_STRIP = re.compile(r"[\s:._–—-]+$")


def canonical_section(heading: str, synonyms: dict[str, str]) -> str | None:
    """Mirror of domain.ts canonicalSection: the canonical section a heading maps
    to, or None when nothing recognises it."""
    key = _HEAD_STRIP.sub("", heading.strip().lower())
    for c in ATS_CANONICAL_SECTIONS:
        if c.lower() == key:
            return c
    return synonyms.get(key)


_MONTH = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
          r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
_TOKEN = rf"(?:(?:{_MONTH})\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_DATE_RANGE = re.compile(rf"^{_TOKEN}(?:\s*[–-]\s*(?:{_TOKEN}|Present))?$", re.I)


def date_range_ok(dates: str) -> bool:
    """Mirror of domain.ts dateRangeOk: one accepted, parseable date family."""
    return bool(_DATE_RANGE.match(dates.strip()))


# --- artifact readers (Python-only I/O) ---------------------------------------


def docx_facts(path: Path) -> dict:
    """What the .docx contains, for the structural rules. Headings are detected as
    our generator emits them: bold, all-caps, short lines with no digits."""
    import docx
    from docx.oxml.ns import qn

    d = docx.Document(str(path))
    body_xml = d.element.body.xml

    normal = d.styles["Normal"].font.size
    headings: list[str] = []
    sizes: list[float] = []
    for p in d.paragraphs:
        t = p.text.strip()
        runs = [r for r in p.runs if r.text.strip()]
        psize = None
        for r in runs:
            s = r.font.size if r.font.size is not None else normal
            if s is not None:
                sizes.append(s.pt)
                psize = s.pt if psize is None else max(psize, s.pt)
        # A section heading in our generator is a short, bold, all-caps line at
        # heading size (~11pt). The >13pt filter keeps the large name line out.
        if t and runs and all(r.bold for r in runs) and t == t.upper() \
                and len(t.split()) <= 4 and not any(ch.isdigit() for ch in t) and "@" not in t \
                and (psize is None or psize <= 13):
            headings.append(t)

    cols = []
    for s in d.sections:
        c = s._sectPr.find(qn("w:cols"))
        num = c.get(qn("w:num")) if c is not None else None
        cols.append(int(num) if num else 1)

    header_footer = []
    for s in d.sections:
        for region in (s.header, s.footer):
            for p in region.paragraphs:
                if p.text.strip():
                    header_footer.append(p.text.strip())

    fonts = {r.font.name for p in d.paragraphs for r in p.runs if r.font.name}
    if d.styles["Normal"].font.name:
        fonts.add(d.styles["Normal"].font.name)

    media = 0
    try:
        for part in d.part.package.iter_parts():
            if "media" in str(part.partname):
                media += 1
    except Exception:  # noqa: BLE001 - absence is the normal case
        pass

    return {
        "tables": len(d.tables),
        "textboxes": body_xml.count("txbxContent") + body_xml.count("v:textbox"),
        "inline_images": len(d.inline_shapes),
        "drawings": body_xml.count("<w:drawing") + body_xml.count("a:blip"),
        "media_parts": media,
        "headings": headings,
        "fonts": fonts,
        "min_pt": min(sizes) if sizes else None,
        "body_text": "\n".join(p.text for p in d.paragraphs),
        "header_footer": header_footer,
        "columns": max(cols) if cols else 1,
    }


def pdf_facts(path: Path) -> dict:
    """What extracting the .pdf yields, for the PDF-integrity rules."""
    from pypdf import PdfReader

    r = PdfReader(str(path))
    text = "\n".join((pg.extract_text() or "") for pg in r.pages)

    images = 0
    fonts: set[str] = set()
    embedded = True
    for pg in r.pages:
        res = pg.get("/Resources")
        if not res:
            continue
        xobj = res.get("/XObject")
        if xobj:
            for o in xobj.get_object().values():
                if o.get_object().get("/Subtype") == "/Image":
                    images += 1
        fnt = res.get("/Font")
        if fnt:
            for fo in fnt.get_object().values():
                fobj = fo.get_object()
                base = fobj.get("/BaseFont")
                if base:
                    fonts.add(str(base).lstrip("/").split("+")[-1])  # drop subset prefix
                desc = fobj.get("/FontDescriptor")
                if desc and not any(k in desc.get_object()
                                    for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    embedded = False

    return {
        "text": text,
        "word_count": len(text.split()),
        "images": images,
        "has_cid": "(cid:" in text,
        "has_fffd": "�" in text,
        "has_bullet": "•" in text,
        "fonts": fonts,
        "fonts_embedded": embedded,
    }


# --- check primitives ---------------------------------------------------------
# Each takes (params, ctx) and returns (passed, detail). ctx has 'docx', 'pdf',
# 'cv'. A missing artifact yields a skipped-but-passing result so an absent PDF
# never falsely blocks a DOCX.


def _emails(text: str) -> bool:
    return bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", text))


def _p_no_tables(params, ctx):
    n = ctx["docx"]["tables"]
    return (n == 0, f"{n} table(s) in the document")


def _p_no_text_boxes(params, ctx):
    n = ctx["docx"]["textboxes"]
    return (n == 0, "text box(es) in the document")


def _p_no_images(params, ctx):
    d = ctx["docx"]["inline_images"] + ctx["docx"]["drawings"] + ctx["docx"]["media_parts"]
    p = ctx["pdf"]["images"] if ctx["pdf"] else 0
    return (d == 0 and p == 0, f"images found (docx={d}, pdf={p})")


def _p_no_header_footer_content(params, ctx):
    hf = ctx["docx"]["header_footer"]
    return (not hf, f"header/footer carries text: {hf[:1]}")


def _p_single_column(params, ctx):
    c = ctx["docx"]["columns"]
    return (c <= 1, f"{c} text columns")


def _p_contact_in_body(params, ctx):
    text = ctx["docx"]["body_text"] + "\n" + (ctx["pdf"]["text"] if ctx["pdf"] else "")
    return (_emails(text), "no email address found in the body text")


def _p_canonical_section_headings(params, ctx):
    syn = {k.lower(): v for k, v in (params.get("synonyms") or {}).items()}
    bad = [h for h in ctx["docx"]["headings"] if canonical_section(h, syn) is None]
    return (not bad, "unrecognised heading(s): " + ", ".join(bad))


def _p_date_format_canonical(params, ctx):
    bad = []
    for role in (ctx["cv"].get("experience") or []):
        dates = str(role.get("dates", "")).strip()
        if dates and not date_range_ok(dates):
            bad.append(dates)
    return (not bad, "unparseable date(s): " + "; ".join(bad))


def _p_pdf_selectable_text(params, ctx):
    if not ctx["pdf"]:
        return (True, "")
    wc = ctx["pdf"]["word_count"]
    return (wc >= int(params.get("min_words", 150)), f"only {wc} words extracted (likely image/scanned)")


def _p_pdf_no_bad_glyphs(params, ctx):
    if not ctx["pdf"]:
        return (True, "")
    bad = ctx["pdf"]["has_cid"] or ctx["pdf"]["has_fffd"]
    return (not bad, "extracted text has (cid:) tokens or replacement characters")


def _p_pdf_bullet_extracts(params, ctx):
    if not ctx["pdf"]:
        return (True, "")
    # Only meaningful when the resume uses bullets; our generator always does.
    return (ctx["pdf"]["has_bullet"], "bullet glyph did not extract as text")


def _p_web_safe_font(params, ctx):
    # Match by family, ignoring spaces and bold/italic style suffixes: a PDF's
    # "Calibri-Bold"/"calibribold" is still the allowed Calibri family.
    allowed = [a.lower().replace(" ", "") for a in (params.get("allowed") or [])]
    used = set(ctx["docx"]["fonts"]) | (set(ctx["pdf"]["fonts"]) if ctx["pdf"] else set())
    off = []
    for f in used:
        fn = (f or "").lower().replace(" ", "")
        if fn and not any(a in fn or fn in a for a in allowed):
            off.append(f)
    return (not off, "non-web-safe font(s): " + ", ".join(sorted(off)))


def _p_body_font_min(params, ctx):
    m = ctx["docx"]["min_pt"]
    if m is None:
        return (True, "")
    return (m >= float(params.get("min_pt", 10)), f"smallest font is {m}pt")


CHECKS = {
    "no_tables": _p_no_tables,
    "no_text_boxes": _p_no_text_boxes,
    "no_images": _p_no_images,
    "no_header_footer_content": _p_no_header_footer_content,
    "single_column": _p_single_column,
    "contact_in_body": _p_contact_in_body,
    "canonical_section_headings": _p_canonical_section_headings,
    "date_format_canonical": _p_date_format_canonical,
    "pdf_selectable_text": _p_pdf_selectable_text,
    "pdf_no_bad_glyphs": _p_pdf_no_bad_glyphs,
    "pdf_bullet_extracts": _p_pdf_bullet_extracts,
    "web_safe_font": _p_web_safe_font,
    "body_font_min": _p_body_font_min,
}


# --- engine -------------------------------------------------------------------


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("rules") or []


def check_cv(docx_path: Path | None, pdf_path: Path | None, cv: dict | None,
             rules: list[dict] | None = None) -> dict:
    """Run the ruleset against a CV's artifacts. Returns
    {verdict, sendable, findings:[{ruleId,severity,passed,detail}]}. An artifact
    that is absent or unreadable makes its own rules fail with a clear detail,
    never a crash."""
    if rules is None:
        rules = load_rules()
    ctx = {
        "docx": docx_facts(docx_path) if docx_path and Path(docx_path).exists() else {},
        "pdf": pdf_facts(pdf_path) if pdf_path and Path(pdf_path).exists() else {},
        "cv": cv or {},
    }
    findings = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        fn = CHECKS.get(rule.get("check", ""))
        severity = rule.get("severity", "warning")
        if fn is None:
            findings.append({"ruleId": rule.get("id", "?"), "severity": severity,
                             "passed": False, "detail": f"no engine primitive '{rule.get('check')}'"})
            continue
        try:
            passed, detail = fn(rule.get("params") or {}, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken artifact must fail, not crash
            passed, detail = False, f"check error: {exc}"
        findings.append({"ruleId": rule["id"], "severity": severity,
                         "passed": bool(passed), "detail": "" if passed else detail})
    return {"verdict": ats_verdict(findings), "sendable": ats_sendable(findings), "findings": findings}


def check_uid(uid: str, cfg: dict) -> dict | None:
    """Check a posting's drafted CV, reading the artifact paths from its state
    record. None when the posting has no drafted CV."""
    import rolegenie

    state_path = ROOT / cfg["output"]["state_file"]
    cvrec = (rolegenie.load_state(state_path).get(uid) or {}).get("cv") or {}
    if cvrec.get("status") != "drafted":
        return None
    docx_path = Path(cvrec["docx"]) if cvrec.get("docx") else None
    pdf_path = Path(cvrec["pdf"]) if cvrec.get("pdf") else None
    cv = {}
    if cvrec.get("folder"):
        cv_json = Path(cvrec["folder"]) / "cv.json"
        if cv_json.exists():
            cv = json.loads(cv_json.read_text(encoding="utf-8"))
    return check_cv(docx_path, pdf_path, cv)


def _render_report(report: dict) -> str:
    lines = [f"verdict: {report['verdict']}  (sendable: {report['sendable']})"]
    for f in report["findings"]:
        mark = "ok " if f["passed"] else ("XX " if f["severity"] == "blocker" else "!! ")
        lines.append(f"  {mark}{f['ruleId']}" + (f" - {f['detail']}" if f["detail"] else ""))
    return "\n".join(lines)


def cmd_ats_check(argv: list[str], cfg: dict) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="rolegenie.py ats-check")
    ap.add_argument("--uid", help="check this posting's drafted CV")
    ap.add_argument("--docx", help="check an explicit .docx")
    ap.add_argument("--pdf", help="check an explicit .pdf")
    ap.add_argument("--cv", help="cv.json for the --docx/--pdf content (dates)")
    args = ap.parse_args(argv)

    if args.uid:
        report = check_uid(args.uid, cfg)
        if report is None:
            print(f"{args.uid}: no drafted CV", file=sys.stderr)
            return 1
    else:
        cv = json.loads(Path(args.cv).read_text(encoding="utf-8")) if args.cv else {}
        report = check_cv(Path(args.docx) if args.docx else None,
                          Path(args.pdf) if args.pdf else None, cv)
    print(_render_report(report))
    return 0 if report["sendable"] else 2
