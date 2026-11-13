"""Tailored CV per posting (arch.md §7, RG-S03).

For each scored posting worth a look that has no CV yet, draft a tailored CV
from the master CV through the configured subscription CLI, check it against
the master (INV-RG-05), and write .docx, .pdf, the JD and the alignment report
to OneDrive\\Jobs\\<Company>-<Role>\\. Contact details are taken from the
master locally and never sent to the model (INV-RG-08).

    python rolegenie.py tailor [--limit N] [--uid UID]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path

import docx
from docx.shared import Cm, Pt

import rolegenie

ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / "prompts" / "tailor_v4.md"
# CR-rg-user-data-home: adverts live in the user's data dir, resolved when used. None in
# production; a test may point it at a tmp path.
JD_DIR: Path | None = None


def _jd_dir() -> Path:
    return JD_DIR or rolegenie.data_dir() / "jd"
EXPERIENCE_HEADING = "PROFESSIONAL EXPERIENCE"


def cert_link_for(cert: str, links: list | None) -> str | None:
    """Mirror of contracts/rolegenie/domain.ts certLinkFor: the verification URL for a
    cert line, or None. First link whose every token is present (case-insensitive) wins
    (CR-rg-cert-links)."""
    hay = cert.lower()
    for link in (links or []):
        toks = link.get("match") or []
        if toks and all(str(t).lower() in hay for t in toks):
            return link.get("url")
    return None


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    """Append an external hyperlink run to a docx paragraph (python-docx has no native
    helper), styled blue + underlined so a reader sees it is verifiable."""
    from docx.oxml.shared import OxmlElement, qn
    r_id = paragraph.part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); rpr.append(color)
    underline = OxmlElement("w:u"); underline.set(qn("w:val"), "single"); rpr.append(underline)
    run.append(rpr)
    t = OxmlElement("w:t"); t.text = text; run.append(t)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


# ---------------------------------------------------------------------------
# JD capture
# ---------------------------------------------------------------------------

def _safe(text: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:limit] or "x"


# Tailored CV file names (CR-rg-tailor-cv-name; mirror of CV_FILE_* / cvFileStem in
# contracts/rolegenie/domain.ts). Stem <name>_<title>_<board>; the name is the master CV's own:
# first line, cut at the first "|", stripped; "CV" when empty or holding "@" or any decimal digit;
# a whole stem that is a Windows reserved device name gets a "CV_" prefix (guard). CR-rg-cv-name-accents: the name keeps Unicode letters, marks and
# digits (NFC); title and board keep the ASCII `_safe` rule.
CV_FILE_PREFIX_DEFAULT = "CV"
CV_FILE_STEM_PARTS = ("name", "title", "board")
CV_FILE_STEM_LIMITS = {"name": 40, "title": 50, "board": 30}
CV_FILE_NAME_MAX_BYTES = 120
CV_FILE_RESERVED_NAMES = frozenset((
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "COM¹", "COM²", "COM³",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9", "LPT¹", "LPT²", "LPT³"))


def cv_file_name_safe(text: str, limit: int) -> str:
    """Mirror of cvFileNameSafe: per character, since `re` has no \\p{} and isalnum drops marks."""
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" else "_" for c in unicodedata.normalize("NFC", text))
    points = list(re.sub(r"_+", "_", kept).strip("_")[:limit])
    while len("".join(points).encode("utf-8")) > CV_FILE_NAME_MAX_BYTES:
        points.pop()
    return "".join(points)


def cv_file_stem(name: str, title: str, board: str) -> str:
    first = re.split(r"\r?\n", name or "")[0].split("|")[0].strip()
    if "@" in first or any(unicodedata.category(c) == "Nd" for c in first):
        prefix = ""
    else:
        prefix = cv_file_name_safe(first, CV_FILE_STEM_LIMITS["name"])
    parts = {"name": prefix or CV_FILE_PREFIX_DEFAULT,
             "title": _safe(title or "", CV_FILE_STEM_LIMITS["title"]),
             "board": _safe(board or "", CV_FILE_STEM_LIMITS["board"])}
    joined = "_".join(parts[k] for k in CV_FILE_STEM_PARTS)
    return f"{CV_FILE_PREFIX_DEFAULT}_{joined}" if cv_file_reserved(joined) else joined   # whole-stem guard only


def cv_file_reserved(stem: str) -> bool:
    """Mirror of cvFileReserved: the whole stem is a Windows reserved device name (ASCII case-insensitive)."""
    return re.sub(r"[a-z]", lambda m: m.group().upper(), stem) in CV_FILE_RESERVED_NAMES


def jd_path(uid: str) -> Path:
    return _jd_dir() / f"{_safe(uid, 120)}.txt"


def jd_decomp_path(uid: str) -> Path:
    """The per-advert decomposition record (CR-rg-jd-decompose), beside the JD."""
    return _jd_dir() / f"{_safe(uid, 120)}.json"


def save_jd(p: rolegenie.Posting) -> None:
    """Keep the advert text; it is needed for tailoring and the board's JD view."""
    if p.body:
        _jd_dir().mkdir(parents=True, exist_ok=True)
        jd_path(p.uid).write_text(p.body, encoding="utf-8")


def write_decomposition(p: rolegenie.Posting) -> None:
    """Write the per-advert decomposition record jd/<uid>.json (CR-rg-jd-decompose).
    Gitignored like the JD text, so no advert or recruiter data is committed
    (INV-RG-08). Skipped when nothing was extracted."""
    if not p.requirements:
        return
    _jd_dir().mkdir(parents=True, exist_ok=True)
    record = {
        "uid": p.uid,
        "advert_sha": (p.decompose or {}).get("advert_sha", rolegenie.advert_sha(p.body)),
        "extracted_at": (p.decompose or {}).get("at"),
        "model": (p.decompose or {}).get("model"),
        "header_facts": {"title": p.title, "location": p.location, "board": p.board,
                         "category": p.category, "ceiling": p.ceiling,
                         **{k: (p.facts or {}).get(k) for k in ("ir35", "day_rate", "length", "remote")}},
        "requirements": p.requirements,
    }
    jd_decomp_path(p.uid).write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_missing_jds(uids: list[str], state: dict, cfg: dict) -> None:
    """Re-read the boards for postings whose JD was never saved. A posting that
    is no longer listed stays without a JD and is skipped."""
    boards = {state[u].get("board") for u in uids}
    wanted = [b for b in cfg["boards"] if b.get("name") in boards]
    if not wanted:
        return
    postings, _ = rolegenie.fetch_all(wanted)
    need = [p for p in postings if p.uid in set(uids)]
    rolegenie.hydrate(need)
    for p in need:
        save_jd(p)


# ---------------------------------------------------------------------------
# Master CV
# ---------------------------------------------------------------------------

def read_master(path: Path) -> tuple[str, str, str, list[dict]]:
    """Return (name, contact line, text without contact, role headings)."""
    paras = [p.text.strip() for p in docx.Document(str(path)).paragraphs if p.text.strip()]
    contact = next(p for p in paras if "@" in p)
    roles, in_exp = [], False
    for p in paras:
        if p == EXPERIENCE_HEADING:
            in_exp = True
            continue
        if in_exp and p.isupper() and "|" not in p:
            break
        if in_exp and p.count(" | ") >= 2:
            parts = p.split(" | ")
            roles.append({"employer": parts[0], "title": " | ".join(parts[1:-1]), "dates": parts[-1]})
    text = "\n".join(p for p in paras if p != contact)
    return paras[0], contact, text, roles


def master_fingerprint(master_text: str) -> str:
    """Identifies the version of the master a CV was drafted from."""
    import hashlib
    return hashlib.sha256(master_text.encode("utf-8")).hexdigest()[:16]


def current_master_sha(cfg: dict) -> str:
    # CR-rg-card-completeness: the scoring profile (and the distilled card) is the master CV
    # PLUS the owner's profile notes, so the fingerprint must cover BOTH. Keying on the CV text
    # alone meant a notes-only enrichment never changed the sha — the stale card was reused and
    # scores were never re-done. raw_profile assembles the same two sources the card distils from.
    master_text = read_master(Path(cfg["tailor"]["master"]))[2]
    notes_path = rolegenie.notes_path()  # the same file the card reads (CR-rg-user-data-home)
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    return master_fingerprint(master_text + "\n\n" + notes)


# ---------------------------------------------------------------------------
# Checks (mirror of contracts/rolegenie/domain.ts cvProblems)
# ---------------------------------------------------------------------------

# CR-rg-tailor-v2: wording that admits or hedges a gap (mirrors GAP_WORDING in domain.ts).
GAP_WORDING = re.compile(
    r"\bnot yet (?:part of|used|worked|had|hands-on|certified|experienced)\b|\bhands-on record\b"
    r"|\b(?:limited|minimal|little|some|basic|initial|no) (?:direct |prior |previous |formal |hands-on |production |commercial |working )?"
    r"(?:experience|exposure|knowledge)\b(?!-)|\b(?:limited|minimal|some|basic|initial) exposure to\b"
    r"|\bfamiliar(?:ity)? with\b|\bcurrently learning\b|\btransferable\b|\(training\)|\bnot a certification\b"
    r"|\bnot (?:currently |yet )?(?:sc|dv|security)[ -]?cleared\b|\buncleared\b|\bdo(?:es)? not hold (?:an? |any )?(?:sc|dv|security)? ?clearance\b|\b(?:have|has) not used\b|\bnever used\b|\bnot (?:yet )?certified\b|\bwithout (?:any |direct |prior |formal |hands-on )?(?:experience|exposure)\b|\black(?:s|ing)? (?:of )?(?:direct |prior |hands-on )?(?:experience|exposure|knowledge)\b|(?:^|\n)\s*new to\b|\b(?:am|relatively|still|being|fairly|completely) new to\b|\b(?:keen|eager|willing) to learn\b",
    re.I)


def numbers_in(text: str) -> list[str]:
    return [n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,.]*", text)]


def cv_problems(cv: dict, master_text: str, master_roles: list[dict],
                knowledge_only: list[str] | tuple = ()) -> list[str]:
    problems = []
    key = lambda r: f"{r.get('employer')} | {r.get('title')} | {r.get('dates')}"  # noqa: E731
    order = [key(r) for r in master_roles]
    last = -1
    for r in cv.get("experience", []):
        at = order.index(key(r)) if key(r) in order else -1
        if at < 0:
            problems.append(f"role heading not in the master: {key(r)}")
        elif at < last:
            problems.append(f"role out of the master's order: {key(r)}")
        else:
            last = at
    kept = {key(r) for r in cv.get("experience", [])}
    problems += [f"role missing from the CV: {k}" for k in order if k not in kept]   # CR-rg-tailor-v2
    written = "\n".join([cv.get("headline", ""), *cv.get("profile", []), *cv.get("core_expertise", []),
                         *(b for r in cv.get("experience", []) for b in r.get("bullets", [])),
                         *(f"{w.get('name', '')} {w.get('text', '')}" for w in cv.get("selected_work", []))])
    text = "\n".join([written, *cv.get("certifications", []), *cv.get("education", [])])
    have = set(numbers_in(master_text))
    for n in dict.fromkeys(numbers_in(text)):
        if n not in have:
            problems.append(f"number not in the master: {n}")
    if re.search(r"@|\+44|\b07\d{3}\s?\d{6}\b", text):
        problems.append("contact details must not come from the model")
    if re.search(r"\bthe candidate\b", text, re.I):
        problems.append('write in the implied first person, not "the candidate"')
    if GAP_WORDING.search(written):   # certifications and education are verbatim from the master
        problems.append("state no gap or hedge in the CV; gaps go only in the report")
    # CR-rg-knowledge-only: known but not recently delivered: listed plainly as a capability, never as
    # delivered work. Delivered-work sections may not name the term at all; the headline, profile and
    # core expertise may not pair it with a delivery verb.
    delivered = _norm("\n".join([*(b for r in cv.get("experience", []) for b in r.get("bullets", [])),
                                 *(f"{w.get('name', '')} {w.get('text', '')}" for w in cv.get("selected_work", []))]))
    claims = [_norm(c) for line in [cv.get("headline", ""), *cv.get("profile", []), *cv.get("core_expertise", [])]
              for c in re.split(r"[.;:]", line)]
    for k in knowledge_only:
        nk = _norm(k)
        if nk and (nk in delivered or any(nk in c and DELIVERY_VERB.search(c) for c in claims)):
            problems.append(f"{k} is knowledge only: list it as a capability, not as delivered work")
    return problems


# Mirror of contracts/rolegenie/domain.ts DELIVERY_VERB (CR-rg-knowledge-only).
DELIVERY_VERB = re.compile(r"\b(built|build|builds|delivered|deliver|delivers|shipped|ships|deployed|deploys|"
                           r"implemented|implements|developed|develops|created|launched|ran|runs|operated|"
                           r"configured|set up|stood up|productionised|productionized|migrated)\b")


def _norm(text: str) -> str:
    """Lower case, hyphens and runs of spaces as single spaces, so "AI-Foundry" matches "ai foundry"."""
    return re.sub(r"[\s-]+", " ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

APPROVED_MARK, SUPPORT_MARK = "<!-- approved-mode -->", "<!-- support-check -->"


def _prompt_section(which: str | None) -> str:
    """tailor_v4.md holds three prompts: the legacy CV prompt (the head, byte-for-byte as before BRIEF-049), the
    approved-fact drafting prompt and the claim support-check prompt, each after its marker."""
    text = PROMPT.read_text(encoding="utf-8")
    head, _, rest = text.partition("\n" + APPROVED_MARK)
    if which is None:
        return head
    approved, _, support = rest.partition(SUPPORT_MARK)
    return (approved if which == "approved" else support).strip("\n") + "\n"


def build_prompt(entry: dict, jd: str, master_text: str, roles: list[dict],
                 knowledge_only: list[str] | tuple = ()) -> str:
    fills = {
        "{knowledge_only}": ", ".join(knowledge_only) or "(none)",
        "{roles}": "\n".join(f"- {r['employer']} | {r['title']} | {r['dates']}" for r in roles),
        "{master}": master_text,
        "{company}": entry.get("board", ""),
        "{title}": (entry.get("title") or "").strip(),
        "{location}": entry.get("location", ""),
        "{jd}": jd[:20000],
    }
    out = _prompt_section(None)
    for k, v in fills.items():
        out = out.replace(k, v)
    return out


def draft(prompt: str, cfg: dict, attempts: int = 3) -> dict:
    """Draft a CV as JSON. Parses tolerantly (control characters allowed) and retries
    the model on a parse failure, so a transient malformed generation self-heals rather
    than failing the whole tailor (CR-rg-json-robustness)."""
    last: Exception | None = None
    for _ in range(attempts):
        out = rolegenie.run_model(prompt, cfg, timeout=600)
        try:
            return rolegenie.parse_model_json(out)
        except ValueError as exc:  # JSONDecodeError and a missing '{' both raise ValueError
            last = exc
            print(f"  draft JSON unparseable, retrying: {exc}", file=sys.stderr)
    raise last if last else ValueError("the drafter returned no output")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _role_head(role: dict) -> tuple[str, str]:
    """(bold part, rest) of a role line. An approved-fact role carries its heading verbatim (BRIEF-049); a legacy role
    is `title, employer | dates`."""
    if role.get("heading"):
        return role["heading"], ""
    return f"{role['title']}, {role['employer']}", f" | {role['dates']}"


def render_docx(cv: dict, name: str, contact: str, path: Path, cert_links: list | None = None) -> None:
    d = docx.Document()
    for s in d.sections:
        s.page_width, s.page_height = Cm(21.0), Cm(29.7)      # A4, not the template's Letter
        s.top_margin = s.bottom_margin = Cm(1.8)
        s.left_margin = s.right_margin = Cm(1.9)
    # Least metadata (owner, 2026-09-27): no author, title or other properties.
    cp = d.core_properties
    for prop in ("author", "last_modified_by", "title", "subject", "keywords", "comments",
                 "category", "content_status", "identifier", "language", "version"):
        setattr(cp, prop, "")
    style = d.styles["Normal"]
    style.font.name = "Arial"  # metric-identical to the PDF's bundled Liberation Sans
    style.font.size = Pt(10.5)

    def heading(text: str) -> None:
        p = d.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        r = p.add_run(text.upper())
        r.bold = True
        r.font.size = Pt(11)

    p = d.add_paragraph()
    r = p.add_run(name)
    r.bold = True
    r.font.size = Pt(18)
    p = d.add_paragraph()
    p.add_run(cv.get("headline", "")).bold = True
    d.add_paragraph(contact)

    heading("Profile")
    d.add_paragraph(" ".join(cv.get("profile", [])))
    if cv.get("core_expertise"):
        heading("Core expertise")
        d.add_paragraph(" | ".join(cv["core_expertise"]))
    heading("Experience")
    for role in cv.get("experience", []):
        p = d.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        bold, rest = _role_head(role)
        p.add_run(bold).bold = True
        if rest:
            p.add_run(rest)
        for b in role.get("bullets", []):
            d.add_paragraph(b, style="List Bullet")
    if cv.get("selected_work"):
        heading("Selected Projects")  # a recognised ATS section name (CR-rg-ats-check)
        for w in cv["selected_work"]:
            p = d.add_paragraph(style="List Bullet")
            p.add_run(f"{w['name']}: ").bold = True
            p.add_run(w["text"])
    for title, key in (("Certifications", "certifications"), ("Education", "education")):
        if cv.get(key):
            heading(title)
            for line in cv[key]:
                url = cert_link_for(line, cert_links) if key == "certifications" else None
                if url:
                    _add_hyperlink(d.add_paragraph(), url, line)  # clickable, verifiable
                else:
                    d.add_paragraph(line)
    d.save(str(path))


FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"  # bundled, OFL (CR-rg-platform-baseline)


def render_pdf(cv: dict, name: str, contact: str, path: Path, cert_links: list | None = None) -> None:
    """The same CV as render_docx, drawn straight to an A4 PDF. No Word: the
    07:00 run happens with nobody logged in, where Office automation is
    unsupported."""
    from fpdf import FPDF

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(19, 18, 19)
    pdf.set_auto_page_break(True, margin=18)
    for style, face in (("", "Regular"), ("B", "Bold"), ("I", "Italic")):
        pdf.add_font("LiberationSans", style, str(FONT_DIR / f"LiberationSans-{face}.ttf"))
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def text(t: str, size: float = 10.5, style: str = "", after: float = 1.2, link: str = "") -> None:
        pdf.set_font("LiberationSans", style, size)
        if link:
            pdf.set_text_color(5, 99, 193)  # blue: a verifiable, clickable link
        pdf.multi_cell(width, size * 0.45, t, align="L", new_x="LMARGIN", new_y="NEXT", link=link)
        if link:
            pdf.set_text_color(0, 0, 0)
        pdf.ln(after)

    def heading(t: str) -> None:
        pdf.ln(2.5)
        text(t.upper(), 11, "B", 0.8)

    def bullet(t: str, lead: str = "") -> None:
        pdf.set_font("LiberationSans", "", 10.5)
        pdf.set_x(pdf.l_margin + 2)
        pdf.cell(4, 4.7, "\u2022")
        body = f"**{lead}**{t}" if lead else t
        pdf.multi_cell(width - 6, 4.7, body, markdown=bool(lead), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.6)

    text(name, 18, "B", 1)
    text(cv.get("headline", ""), 11, "B", 0.5)
    text(contact, 10, "", 1)
    heading("Profile")
    text(" ".join(cv.get("profile", [])))
    if cv.get("core_expertise"):
        heading("Core expertise")
        text(" | ".join(cv["core_expertise"]))
    heading("Experience")
    for role in cv.get("experience", []):
        pdf.ln(1.2)
        pdf.set_font("LiberationSans", "B", 10.5)
        pdf.multi_cell(width, 4.8, "".join(_role_head(role)),
                       align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.6)
        for b in role.get("bullets", []):
            bullet(b)
    if cv.get("selected_work"):
        heading("Selected Projects")  # a recognised ATS section name (CR-rg-ats-check)
        for w in cv["selected_work"]:
            bullet(w["text"], f"{w['name']}: ")
    for title, key in (("Certifications", "certifications"), ("Education", "education")):
        if cv.get(key):
            heading(title)
            for line in cv[key]:
                url = cert_link_for(line, cert_links) if key == "certifications" else None
                text(line, after=0.6, link=url or "")
    pdf.output(str(path))


def to_pdf(docx_path: Path, cv: dict | None = None, name: str = "", contact: str = "",
           cert_links: list | None = None) -> Path | None:
    """Write the PDF next to the .docx from the same CV content, then strip its
    metadata (owner rule: least metadata). None if it cannot be written."""
    pdf = docx_path.with_suffix(".pdf")
    try:
        if cv is None:
            cv = json.loads((docx_path.parent / "cv.json").read_text(encoding="utf-8"))
        render_pdf(cv, name, contact, pdf, cert_links)
        strip_pdf_metadata(pdf)
    except Exception as exc:  # noqa: BLE001
        print(f"  PDF failed: {exc}", file=sys.stderr)
        return None
    return pdf


def strip_pdf_metadata(pdf: Path) -> None:
    """Remove the document-information dictionary and the XMP metadata stream.
    Text, fonts and the structure tags ATS parsers read are kept."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject

    writer = PdfWriter(clone_from=str(pdf))
    root = writer._root_object
    if NameObject("/Metadata") in root:
        del root[NameObject("/Metadata")]
    writer._info = None
    tmp = pdf.with_suffix(".tmp.pdf")
    with tmp.open("wb") as fh:
        writer.write(fh)
    tmp.replace(pdf)


def pdf_metadata(pdf: Path) -> dict:
    """What metadata a PDF still carries (empty dict = none)."""
    from pypdf import PdfReader

    r = PdfReader(str(pdf))
    meta = {k: str(v) for k, v in (r.metadata or {}).items()}
    if "/Metadata" in r.trailer["/Root"]:
        meta["xmp"] = "present"
    return meta


def job_folder(jobs_dir: Path, entry: dict, uid: str) -> Path:
    """<Company>-<Role>. A folder that exists but belongs to something else
    (an earlier manual application) is never written into."""
    base = jobs_dir / f"{_safe(entry.get('board', ''), 30)}-{_safe(entry.get('title', ''), 60)}"
    for folder in (base, base.with_name(f"{base.name}-{_safe(uid.split(':')[-1], 20)}")):
        marker = folder / ".rolegenie-uid"
        if not folder.exists() or (marker.exists() and marker.read_text(encoding="utf-8").strip() == uid):
            folder.mkdir(parents=True, exist_ok=True)
            marker.write_text(uid, encoding="utf-8")
            return folder
    raise RuntimeError(f"no free folder for {uid}")


# ---------------------------------------------------------------------------
# One posting
# ---------------------------------------------------------------------------

def tailor_one(uid: str, entry: dict, cfg: dict) -> dict:
    """Draft, check and write one CV. Returns the `cv` record for state."""
    if rolegenie.approved_mode():   # BRIEF-049: the user approved a master; never fall back to the legacy DOCX
        return tailor_approved(uid, entry, cfg)
    tc = cfg["tailor"]
    name, contact, master_text, roles = read_master(Path(tc["master"]))
    jd = jd_path(uid).read_text(encoding="utf-8")
    knowledge_only = tc.get("knowledge_only") or []
    prompt = build_prompt(entry, jd, master_text, roles, knowledge_only)

    cv, problems = None, []
    for _attempt in range(2):
        cv = draft(prompt, cfg)
        problems = cv_problems(cv, master_text, roles, knowledge_only)
        if not problems:
            break
        prompt += ("\n\n## CORRECTIONS REQUIRED\nYour previous answer broke these rules. "
                   "Fix every one and answer again with the full JSON:\n- " + "\n- ".join(problems))

    folder = job_folder(Path(tc["jobs_dir"]), entry, uid)
    stem = cv_file_stem(name, entry.get("title", ""), entry.get("board", ""))
    (folder / "JD.md").write_text(
        f"# {entry.get('title', '').strip()} — {entry.get('board', '')}\n\n"
        f"{entry.get('location', '')}\n\n{entry.get('url', '')}\n\n{jd}\n", encoding="utf-8")
    (folder / f"{stem}_Alignment_Report.md").write_text(str(cv.get("report", "")), encoding="utf-8")
    cv_json = {k: v for k, v in cv.items() if k != "report"}
    (folder / "cv.json").write_text(json.dumps(cv_json, indent=2, ensure_ascii=False), encoding="utf-8")
    record = {"folder": str(folder), "at": dt.datetime.now().isoformat(timespec="seconds"),
              # The stamp MUST be the same fingerprint wants_cv/is_queued/the
              # package compare against (master + profile notes), or a fresh CV
              # is instantly "stale" whenever notes exist (BRIEF-017 regression).
              "master_sha": current_master_sha(cfg),
              "prompt": PROMPT.name, "scorer": cfg["scoring"].get("scorer")}
    if problems:
        # Not rendered: a CV that fails the master check is never offered for use.
        return {**record, "status": "failed", "problems": problems}
    docx_path = folder / f"{stem}.docx"
    links = tc.get("cert_links")
    render_docx(cv, name, contact, docx_path, links)
    pdf = to_pdf(docx_path, cv_json, name, contact, links)
    return {**record, "status": "drafted", "docx": str(docx_path), "pdf": str(pdf) if pdf else None}


# ---------------------------------------------------------------------------
# Approved-fact CV (BRIEF-049, CR-rg-approved-fact-cv; contract: contracts/rolegenie/approved-cv.ts)
# ---------------------------------------------------------------------------
# Every factual line is a claim: its text plus the approved fact ids that support it. Code checks each claim against ONLY
# its cited facts (claim_problems); one model support check judges what words cannot (a misleading paraphrase, or a bullet
# moved to the wrong employer). Nothing usable is written unless both pass. Headings and certification lines are copied by
# code from the facts; name and contact come from the user's own resume text and never reach a model.

APPROVED_CV_STATUS = ("drafted", "blocked", "failed")

# Mirror of GUARDED_WORDS in approved-cv.ts (a contract test compares the two lists).
GUARDED_WORDS = [
    "build", "deliver", "ship", "deploy", "implement", "develop", "create", "launch", "run", "operate", "configure",
    "migrate", "productionise", "productionize", "lead", "manage", "own", "architect", "direct", "design", "mentor",
    "oversee", "head", "drive", "spearhead", "establish", "pioneer", "scale", "found", "senior", "principal", "chief",
    "sole", "director", "founder", "handedly",
]
# Mirror of PLAIN_VERBS in approved-cv.ts: common non-ownership CV verbs. A sentence opener with one of these roots, in
# any tense, is not a claimed term (BRIEF-063). A few extra forms are listed because word_root does not fold them.
PLAIN_VERBS = [
    "work", "use", "using", "support", "maintain", "write", "contribute", "help", "collaborate", "partner", "automate",
    "improve", "reduce", "test", "analyse", "analyze", "research", "document", "review", "integrate", "coordinate",
    "train", "teach", "present", "report", "monitor", "optimise", "optimize", "refactor", "debug", "debugging", "fix",
    "resolve", "apply", "applies", "prototype", "define", "evaluate", "assess", "assessing", "investigate", "model",
    "modelling", "extend", "add", "replace", "modernise", "modernize", "streamline", "standardise", "standardize",
    "secure", "consult", "advise", "communicate", "enable", "provide", "handle", "specialise", "specialize", "focus",
    "focusing",
]
_IRREGULAR = {"led": "lead", "leads": "lead", "leading": "lead", "built": "build", "builds": "build",
              "building": "build", "ran": "run", "runs": "run", "running": "run", "drove": "drive", "driven": "drive",
              "oversaw": "oversee", "oversees": "oversee", "overseen": "oversee", "overseeing": "oversee"}
_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "dozen": "dozen",
                 "dozens": "dozen", "hundred": "100", "hundreds": "hundred", "thousand": "1000",
                 "thousands": "thousand", "half": "half", "halve": "half", "halves": "half", "halved": "half",
                 "halving": "half", "double": "double", "doubles": "double", "doubled": "double",
                 "doubling": "double", "twice": "double", "triple": "triple", "triples": "triple",
                 "tripled": "triple", "tripling": "triple", "thrice": "triple", "quadruple": "quadruple",
                 "quadruples": "quadruple", "quadrupled": "quadruple", "quadrupling": "quadruple",
                 "twofold": "twofold", "threefold": "threefold", "fourfold": "fourfold"}
_OPENERS = {"the", "a", "an", "and", "with", "across", "for", "in", "on", "of"}
_CLAIM_CONTACT = re.compile(r"@|\+44|\b07\d{3}\s?\d{6}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+\d[\d ()-]{7,}\d|\b0\d{3,4}[ -]?\d{3}[ -]?\d{3,4}\b")


def word_root(word: str) -> str:
    """Fold tense: led/leads/leading share a root; designed/design/designs share a root."""
    w = word.lower()
    if w in _IRREGULAR:
        w = _IRREGULAR[w]
    else:
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[:-len(suf)]
                break
    return w[:-1] if len(w) > 3 and w.endswith("e") else w


_GUARDED_ROOTS = {word_root(w) for w in GUARDED_WORDS}
_PLAIN_VERB_ROOTS = {word_root(w) for w in PLAIN_VERBS}


def claim_numbers(text: str) -> list[str]:
    """Digit forms (with a %, k, m or bn suffix) and number words, normalised so "four" equals "4"."""
    out = []
    for m in re.finditer(r"\b[xX]\d+\b|\b\d+[xX]\b|\d[\d,.]*(?:%|[kKmM]\b|bn\b)?|[A-Za-z]+", text):
        t = m.group(0)
        times = re.fullmatch(r"[xX](\d+)|(\d+)[xX]", t)
        if times:
            out.append(f"{times.group(1) or times.group(2)}x")
        elif t[0].isdigit():
            out.append(re.sub(r"\.(?=$|\D)", "", t.replace(",", ""), count=1).lower())
        elif t.lower() in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[t.lower()])
    return out


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9+#]*", text)


def claim_problems(claim: dict, kept: list[dict]) -> list[str]:
    """Mirror of claimProblems in approved-cv.ts: why one claim is unsupported by its cited facts ([] = passes)."""
    ids = claim.get("facts")
    text = claim.get("text")
    if not isinstance(text, str) or not text.strip():
        return ["claim has no text"]
    if not isinstance(ids, list) or not ids:
        return ["claim cites no fact"]
    by_id = {f["id"]: f for f in kept}
    cited = [by_id[i] for i in ids if isinstance(i, str) and i in by_id]
    missing = [f"unknown fact {i}" for i in ids if not (isinstance(i, str) and i in by_id)]
    if missing:
        return missing
    fact_text = "\n".join(f["text"] for f in cited)
    fact_words = {t.lower() for t in _tokens(fact_text)}
    fact_roots = {word_root(w) for w in fact_words}
    have = set(claim_numbers(fact_text))
    problems = [f"number not in the cited facts: {n}" for n in dict.fromkeys(claim_numbers(text)) if n not in have]
    seen: set[str] = set()
    for t in _tokens(text):
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        if word_root(t) in _GUARDED_ROOTS:
            if word_root(t) not in fact_roots:
                problems.append(f"upgrades the facts: {t}")
            continue
        if not t[0].isupper() or low in fact_words or low in _OPENERS or low in _NUMBER_WORDS:
            continue
        if ((low.endswith("ed") or word_root(t) in _PLAIN_VERB_ROOTS)
                and re.search(rf"(?:^|[.;:!?]\s+){re.escape(t)}\b", text)):
            continue    # a sentence-opening verb in any tense; ownership verbs are checked above
        problems.append(f"not in the cited facts: {t}")
    if GAP_WORDING.search(text):
        problems.append("state no gap or hedge in the CV; gaps go only in the report")
    if _CLAIM_CONTACT.search(text):
        problems.append("contact details must not come from the model")
    if re.search(r"\bthe candidate\b", text, re.I):
        problems.append('write in the implied first person, not "the candidate"')
    return problems


def approved_cv_due(prior: dict | None, master_sha: str, ad_sha: str, files_present: bool) -> str:
    """Mirror of approvedCvDue: keep only a drafted CV for this approved master and advert whose files still exist."""
    p = prior or {}
    return "keep" if (p.get("status") == "drafted" and p.get("masterSha") == master_sha
                      and p.get("adSha") == ad_sha and files_present) else "draft"


def approved_cv_wanted(latest_decision, prior: dict | None, master_sha: str, ad_sha: str, files_present: bool) -> bool:
    """Mirror of approvedCvWanted: Reject / Consider / Already applied never ask for a draft."""
    return latest_decision == "apply" and approved_cv_due(prior, master_sha, ad_sha, files_present) == "draft"


class CvHold(Exception):
    """The approved CV cannot be drafted yet; the message says what is missing and how to recover."""


def approved_sha() -> str | None:
    """The approved master's hash, or None when it is missing, stale or has an open contradiction."""
    import assess
    import onboard
    try:
        return onboard.master_sha(assess.approved_master())
    except onboard.MasterNotApproved:
        return None


def local_identity(master: dict) -> tuple[str, str, str]:
    """(name, contact line, source file) from the user's own resume text, kept locally. The first resume (master order)
    with an email gives the contact (email plus an optional phone, verbatim) and its name line. Anything doubtful holds."""
    for src in master.get("sources", []):
        lines = [ln.strip().lstrip("#*").strip() for ln in src["extractedText"].splitlines() if ln.strip()]
        at = next((i for i, ln in enumerate(lines) if _EMAIL.search(ln)), None)
        if at is None:
            continue
        name = lines[0]
        words = name.split()
        if (at > 3 or not 2 <= len(words) <= 4 or not all(w.replace("-", "").replace("'", "").isalpha() for w in words)
                or re.search(r"curriculum|resume|\bcv\b", name, re.I)):
            raise CvHold(f"cannot find your name above the email in {src['file']} (it must be a plain 2-4 word name "
                         "within the first lines); fix the resume and run onboard again")
        email = _EMAIL.search(lines[at]).group(0)
        phone = next((m.group(0).strip() for ln in lines[:at + 3] if (m := _PHONE.search(ln))), None)
        return name, " | ".join([email] + ([phone] if phone else [])), src["file"]
    raise CvHold("no email address found in your approved resumes, so there is no contact line to put on the CV; add one "
                 "to a resume and run onboard again (contact details are never taken from anywhere else)")


def _is_contact(text: str) -> bool:
    return bool(_EMAIL.search(text) or _PHONE.search(text))


def _identity(uid: str, entry: dict, jd: str, master_sha: str) -> dict:
    return {"approved": True, "uid": uid, "title": (entry.get("title") or "").strip(), "board": entry.get("board", ""),
            "url": entry.get("url", ""), "adSha": rolegenie.advert_sha(jd), "masterSha": master_sha,
            "master_sha": master_sha, "at": dt.datetime.now().isoformat(timespec="seconds"), "prompt": PROMPT.name}


def _structure(draft: dict, kept: dict[str, dict]) -> tuple[list[tuple[str, str, dict]], list[dict], dict, list[str]]:
    """(claims as (section, role heading, claim), experience, cert/education fact ids, problems) from the model's JSON."""
    problems, claims = [], []

    def claim(section: str, heading: str, c) -> None:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            claims.append((section, heading, {"text": c["text"].strip(), "facts": c.get("facts")}))
        else:
            problems.append(f"{section}: malformed claim")

    claim("headline", "", draft.get("headline"))
    for section in ("profile", "core_expertise"):
        items = draft.get(section) or []
        if not isinstance(items, list):
            problems.append(f"{section}: must be a list")
            continue
        for c in items:
            claim(section, "", c)
    experience = []
    exp = draft.get("experience") or []
    if not isinstance(exp, list) or not exp:
        problems.append("experience: at least one role is required")
        exp = []
    for r in exp:
        role = kept.get(r.get("role")) if isinstance(r, dict) else None
        if role is None or role["category"] != "role":
            problems.append(f"experience: {r.get('role') if isinstance(r, dict) else r!r} is not a role fact")
            continue
        bullets = r.get("bullets") or []
        for b in bullets if isinstance(bullets, list) else []:
            claim("experience", role["text"], b)
        experience.append({"heading": role["text"], "role": role["id"],
                           "bullets": [b["text"] for b in bullets if isinstance(b, dict) and isinstance(b.get("text"), str)]
                           if isinstance(bullets, list) else []})
    lists = {}
    for key, category in (("certifications", "certification"), ("education", "education")):
        ids = draft.get(key) or []
        lists[key] = []
        for i in ids if isinstance(ids, list) else []:
            f = kept.get(i) if isinstance(i, str) else None
            if f is None or f["category"] != category:
                problems.append(f"{key}: {i!r} is not a {category} fact")
            else:
                lists[key].append(f)
    return claims, experience, lists, problems


def support_check(claims: list[tuple[str, str, dict]], kept: dict[str, dict], cfg: dict) -> list[str]:
    """One model call: is every claim supported by ONLY its cited facts, and does it belong under its role? Fails closed."""
    lines = [f"{i}. [{heading or 'none'}] {c['text']} || " + "; ".join(f"{fid}: {kept[fid]['text']}" for fid in c["facts"])
             for i, (_, heading, c) in enumerate(claims)]
    try:
        out = draft(_prompt_section("support").replace("{claims}", "\n".join(lines)), cfg, attempts=2)
    except Exception as exc:  # noqa: BLE001 - any failure of the checker means nothing is offered
        return [f"support check unavailable, nothing offered: {str(exc)[:200]}"]
    verdicts = {v.get("i"): v for v in out.get("verdicts") or [] if isinstance(v, dict)}
    problems = []
    for i, (_, _, c) in enumerate(claims):
        v = verdicts.get(i)
        if not v or v.get("supported") is not True:
            why = str((v or {}).get("why") or "no verdict returned")[:160]
            problems.append(f"claim not supported by its cited facts ({why}): {c['text']}")
    return problems


def _claims_record(claims, kept: dict[str, dict], withheld: list[str], contact_source: str) -> dict:
    def support(fid: str) -> dict:
        f = kept[fid]
        return {"factId": fid, "text": f["text"],
                **({"sources": f["sources"]} if f.get("sources") else {}),
                **({"userConfirmation": f["userConfirmation"]} if f.get("userConfirmation") else {})}
    return {"attribution": "model-judged", "contactSource": contact_source, "withheld": withheld,
            "claims": [{"section": s, "role": h or None, "text": c["text"], "support": [support(i) for i in c["facts"]]}
                       for s, h, c in claims]}


def tailor_approved(uid: str, entry: dict, cfg: dict) -> dict:
    """Draft one CV from the APPROVED master. Returns the `cv` record for state; status drafted | blocked | failed.
    Blocked means a prerequisite is missing (reason says how to recover); failed means no claim set passed the checks.
    Neither writes a usable file, and no owner-profile fallback exists."""
    import assess
    import onboard
    tc = cfg["tailor"]
    jd_file = jd_path(uid)
    jd = jd_file.read_text(encoding="utf-8") if jd_file.exists() else ""
    ident = _identity(uid, entry, jd, "")

    def hold(reason: str, master_sha: str = "") -> dict:
        return {**ident, "masterSha": master_sha, "master_sha": master_sha, "status": "blocked", "reason": reason}

    try:
        master = assess.approved_master()
        sha = onboard.master_sha(master)
        ident = _identity(uid, entry, jd, sha)
        facts = assess.kept_facts(master)
        if not any(f["category"] == "role" for f in facts):
            raise CvHold("the approved master has no role facts, so there is no employment structure to build a CV on; "
                         "add your roles (rolegenie.py master confirm \"...\" --category role), approve again and retry")
        name, contact, contact_source = local_identity(master)
        if not jd.strip():
            raise CvHold("the advert text for this job is not saved, so there is nothing to tailor to; "
                         "run rolegenie.py scan or add the job again")
    except onboard.MasterNotApproved as exc:
        return hold(str(exc))
    except CvHold as exc:
        return hold(str(exc), sha if "sha" in locals() else "")

    prior = entry.get("cv")
    folder = Path(prior["folder"]) if prior and prior.get("folder") else None
    present = bool(folder and Path(prior.get("docx") or "x").exists() and Path(prior.get("claims") or "x").exists())
    if approved_cv_due(prior, sha, ident["adSha"], present) == "keep":
        return prior

    withheld = [f["id"] for f in facts if _is_contact(f["text"])]
    kept = {f["id"]: f for f in facts if f["id"] not in withheld}
    fact_lines = "\n".join(f"{f['id']} [{f['category']}]: {f['text']}" for f in kept.values())
    prompt = (_prompt_section("approved").replace("{facts}", fact_lines)
              .replace("{company}", entry.get("board", "")).replace("{title}", (entry.get("title") or "").strip())
              .replace("{location}", entry.get("location", "")).replace("{jd}", jd[:20000]))
    problems: list[str] = []
    for _attempt in range(2):
        try:
            drafted = draft(prompt, cfg)
        except Exception as exc:  # noqa: BLE001 - a model outage is a visible failure, never a fallback
            problems = [f"the drafting model failed: {str(exc)[:200]}"]
            break
        claims, experience, lists, problems = _structure(drafted, kept)
        for section, _, c in claims:
            problems += [f"{section}: {p}: {c['text']}" for p in claim_problems(c, list(kept.values()))]
        if not problems:
            problems = support_check(claims, kept, cfg)
            if problems and problems[0].startswith("support check unavailable"):
                break
        if not problems:
            break
        prompt += ("\n\n## CORRECTIONS REQUIRED\nYour previous answer broke these rules. "
                   "Fix every one and answer again with the full JSON:\n- " + "\n- ".join(problems))
    if problems:
        return {**ident, "status": "failed", "problems": problems}

    stem = cv_file_stem(name, entry.get("title", ""), entry.get("board", ""))
    folder = job_folder(Path(tc["jobs_dir"]), entry, uid)
    head = next(c for s, _, c in claims if s == "headline")["text"]
    cv = {"headline": head,
          "profile": [c["text"] for s, _, c in claims if s == "profile"],
          "core_expertise": [c["text"] for s, _, c in claims if s == "core_expertise"],
          "experience": experience,
          "certifications": [f["text"] for f in lists["certifications"]],
          "education": [f["text"] for f in lists["education"]], "selected_work": []}
    report = str(drafted.get("report", ""))
    if _is_contact(report):
        report = "(the model's report contained contact details and was withheld)"
    (folder / "JD.md").write_text(
        f"# {entry.get('title', '').strip()} — {entry.get('board', '')}\n\n"
        f"{entry.get('location', '')}\n\n{entry.get('url', '')}\n\n{jd}\n", encoding="utf-8")
    (folder / f"{stem}_Alignment_Report.md").write_text(
        "> Model-written and NOT checked against your facts; read it as a note, not as CV content.\n\n" + report,
        encoding="utf-8")
    (folder / "cv.json").write_text(json.dumps(cv, indent=2, ensure_ascii=False), encoding="utf-8")
    claims_path = folder / "claims.json"
    claims_path.write_text(json.dumps(_claims_record(claims, kept, withheld, contact_source), indent=2,
                                      ensure_ascii=False), encoding="utf-8")
    docx_path = folder / f"{stem}.docx"
    links = tc.get("cert_links")
    render_docx(cv, name, contact, docx_path, links)
    pdf = to_pdf(docx_path, cv, name, contact, links)
    return {**ident, "status": "drafted", "folder": str(folder), "docx": str(docx_path),
            "pdf": str(pdf) if pdf else None, "claims": str(claims_path), "contactSource": contact_source}


def rerender(cfg: dict) -> int:
    """Rebuild every drafted CV's files from cv.json, e.g. after a layout or
    metadata change. The content (already checked against the master) is unchanged."""
    state = rolegenie.load_state(ROOT / cfg["output"]["state_file"])
    legacy: tuple | None = None
    n = 0
    for uid, e in state.items():
        cv = e.get("cv") or {}
        if cv.get("status") != "drafted" or not cv.get("docx"):
            continue
        if cv.get("approved"):   # BRIEF-049: never re-render an approved CV with the legacy master's name and contact
            continue
        if legacy is None:
            legacy = read_master(Path(cfg["tailor"]["master"]))
        name, contact = legacy[0], legacy[1]
        docx_path = Path(cv["docx"])
        cv_json = json.loads((docx_path.parent / "cv.json").read_text(encoding="utf-8"))
        links = cfg["tailor"].get("cert_links")
        render_docx(cv_json, name, contact, docx_path, links)
        pdf = to_pdf(docx_path, cv_json, name, contact, links)
        n += 1
        print(f"  {uid}: {'ok' if pdf else 'PDF failed'}", file=sys.stderr)
    print(f"Re-rendered {n}", file=sys.stderr)
    return 0


MIN_CEILING_TO_TAILOR = 50   # mirror of contracts/rolegenie/domain.ts


def _latest_decision(e: dict) -> str | None:
    """The raw latest board decision (not mapped), so 'applied-externally' stays distinct
    from a genuine 'apply' (CR-rg-cv-on-apply)."""
    ds = e.get("decisions") or []
    return ds[-1].get("decision") if ds else None


def wants_cv(latest_decision: Any, cv: Any, current_master: str) -> bool:
    """Mirror of domain.ts wantsCv (CR-rg-cv-on-apply): a CV is drafted only on a genuine
    Apply with no current-master CV. No resumes until Apply."""
    if latest_decision != "apply":
        return False
    cv = cv or {}
    return cv.get("status") != "drafted" or cv.get("master_sha") != current_master


def wanted(state: dict, current_master: str) -> list[str]:
    """Postings the owner has applied to that still need a CV for the current master:
    best score first (CR-rg-cv-on-apply). A hard-excluded posting is never tailored even
    if somehow applied to (defence in depth; it is also off the review board)."""
    uids = [u for u, e in state.items()
            if wants_cv(_latest_decision(e), e.get("cv"), current_master)
            and not e.get("excluded")]
    return sorted(uids, key=lambda u: (-(state[u].get("score") or 0), state[u].get("first_seen", "")))


def cmd_tailor(argv: list[str], cfg: dict) -> int:
    ap = argparse.ArgumentParser(prog="rolegenie.py tailor",
                                 description="Draft tailored CVs for scored postings.")
    ap.add_argument("--limit", type=int, default=cfg["tailor"].get("max_per_run", 10))
    ap.add_argument("--uid", help="tailor this posting only (even if it already has a CV)")
    ap.add_argument("--rerender", action="store_true",
                    help="rebuild .docx and .pdf of every drafted CV from its cv.json (no model call)")
    args = ap.parse_args(argv)
    if args.rerender:
        return rerender(cfg)

    state_path = ROOT / cfg["output"]["state_file"]
    state = rolegenie.load_state(state_path)
    if rolegenie.model_command(cfg) is None:
        print("No model CLI available - nothing tailored", file=sys.stderr)
        return 1
    if rolegenie.approved_mode():
        sha = approved_sha()
        if sha is None:
            print("The approved master is missing, changed or has an open contradiction - nothing tailored "
                  "(rolegenie.py master show / master approve)", file=sys.stderr)
            return 1
    else:
        sha = None
    uids = [args.uid] if args.uid else wanted(state, sha or current_master_sha(cfg))[:args.limit]
    missing = [u for u in uids if not jd_path(u).exists()]
    if missing:
        fetch_missing_jds(missing, state, cfg)
    done = failed = 0
    for uid in uids:
        entry = state[uid]
        if not jd_path(uid).exists():
            print(f"  {uid}: advert no longer listed, no JD - skipped", file=sys.stderr)
            continue
        print(f"  tailoring {entry.get('board')}: {entry.get('title', '').strip()}", file=sys.stderr)
        try:
            rec = tailor_one(uid, entry, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad posting must not stop the rest
            print(f"    failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        rolegenie.update_state(state_path, lambda st, u=uid, r=rec: st[u].__setitem__("cv", r))
        done += rec["status"] == "drafted"
        failed += rec["status"] == "failed"
        print(f"    {rec['status']}: {rec.get('folder') or rec.get('reason') or '; '.join(rec.get('problems', []))}",
              file=sys.stderr)
    print(f"Tailored {done}, failed {failed}, of {len(uids)}", file=sys.stderr)
    return 0
