"""The exceptions report as a PDF, for printing and for signing off.

A spreadsheet is what somebody works through; a PDF is what gets attached to
an email, walked into a meeting, or filed with the turnover book. It is the
same findings under the same rule as the other two exports -- only what the
auditor marked **Issue** -- so the three cannot say different things about the
same job.

Built on PyMuPDF, which is already here to read the packages. That matters
more than it sounds: a reporting library would have added tens of megabytes to
a program that already takes half a minute to start, for one output format.

Laid out landscape because the useful columns are wide. The full path of every
document is printed under each finding rather than in a column of its own --
truncating it would defeat the point, and the path is what somebody uses to go
and correct the record.
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

from .db import Database
from .report import reportable, status_word

#: Severity colours, matching the workbook and the window.
_COLOUR = {
    "critical": "#C00000",
    "major": "#C55A11",
    "minor": "#BF8F00",
    "info": "#808080",
}

_CSS = """
body { font-family: sans-serif; font-size: 9pt; color: #17191c; }
h1 { font-size: 15pt; margin: 0 0 2pt 0; }
.sub { font-size: 8.5pt; color: #6b7280; margin: 0 0 4pt 0; }
/* A rule above each item rather than a box around it: a border-box that
   breaks across a page leaves an open-ended frame, which reads as damage. */
.item { border-top: 1px solid #c9ced6; padding: 6pt 0 7pt 0; }
.head { margin-bottom: 1pt; }
.n { font-weight: bold; font-size: 10pt; color: #1F3864; }
.sev { font-weight: bold; font-size: 8.5pt; }
.code { font-size: 8.5pt; color: #17191c; }
.where { font-size: 8.5pt; color: #6b7280; }
.subject { font-weight: bold; font-size: 9.5pt; margin-bottom: 1pt; }
.msg { margin-bottom: 1pt; }
.rule { font-size: 8pt; color: #6b7280; }
.path { font-size: 7.5pt; color: #6b7280; }
.note { font-size: 8.5pt; color: #1F3864; margin-top: 2pt; }
.none { color: #6b7280; font-style: italic; }
"""


def _paths(db: Database, row) -> list[str]:
    from .report import paths_for

    return paths_for(db, row)


def _detail(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(data, dict):
        # document_ids is plumbing for the other exports, not something to read.
        return "; ".join(f"{k}={v}" for k, v in data.items() if k != "document_ids")
    return str(data)


def _body(db: Database, project_id: int, rows: list) -> str:
    project = db.one("SELECT name FROM project WHERE id=?", (project_id,))
    name = project["name"] if project else "project"
    source = db.one("SELECT revision, kind FROM aml_source WHERE project_id=?",
                    (project_id,))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    tally = ", ".join(f"{counts[s]} {s}" for s in
                      ("critical", "major", "minor", "info") if counts.get(s))

    said = [f"{len(rows)} finding{'' if len(rows) == 1 else 's'} marked as an issue"]
    if tally:
        said.append(tally)
    said.append(f"produced {date.today().isoformat()}")
    if source and source["revision"]:
        against = f"approved list {source['revision']}"
        if source["kind"] == "bundled":
            against += " (the copy built into WeldAudit)"
        said.append(against)

    out = [f"<h1>{html.escape(name)}</h1>",
           f"<p class='sub'>{html.escape(' &middot; '.join(said))}</p>"]
    # escape() would eat the separator, so it goes back in afterwards.
    out[1] = out[1].replace("&amp;middot;", "&#183;")

    if not rows:
        out.append(
            "<p class='none'>Nothing on this job is marked as an issue. That is "
            "not the same as nothing being wrong: findings the auditor has not "
            "yet reviewed are deliberately left out of this report.</p>")
        return "".join(out)

    # One block per finding, numbered, rather than a table.
    #
    # A table was tried first and read well — until page two, which carried on
    # with no column headings, because the story engine does not repeat a
    # <thead>. A printed exception report gets separated and marked up, so a
    # page that cannot be understood alone is a real fault, not a cosmetic one.
    #
    # Blocks fix that by needing no headings at all, and suit the medium
    # better: the findings quote long sentences and longer Windows paths,
    # neither of which wants a fixed column width. The numbers are what people
    # say out loud — "item 4 is the one we cleared with the mill".
    for n, r in enumerate(rows, start=1):
        colour = _COLOUR.get(r["severity"], "#17191c")
        detail = _detail(r["detail"])
        head = (f"<span class='n'>{n}</span> "
                f"<span class='sev' style='color:{colour}'>"
                f"{html.escape((r['severity'] or '').upper())}</span> "
                f"<span class='code'>{html.escape(r['rule'])}</span>")
        if r["segment"]:
            head += f" <span class='where'>{html.escape(r['segment'])}</span>"
        out.append(f"<div class='item'><div class='head'>{head}</div>")
        if r["subject"]:
            out.append(f"<div class='subject'>{html.escape(r['subject'])}</div>")
        out.append(f"<div class='msg'>{html.escape(r['message'] or '')}</div>")
        if detail:
            out.append(f"<div class='rule'>{html.escape(detail)}</div>")
        for p in _paths(db, r):
            out.append(f"<div class='path'>{html.escape(p)}</div>")
        note = (r["note"] or "") if "note" in r.keys() else ""
        if note:
            out.append(f"<div class='note'>Comment: {html.escape(note)}</div>")
        out.append("</div>")
    return "".join(out)


def write_pdf(db: Database, project_id: int, path: str | Path) -> Path:
    """Write the exceptions report as a printable PDF."""
    import pymupdf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = reportable(db, project_id)

    story = pymupdf.Story(html=_body(db, project_id, rows), user_css=_CSS)
    writer = pymupdf.DocumentWriter(str(path))
    page = pymupdf.paper_rect("letter-l")          # landscape: the text is wide
    where = page + (36, 36, -36, -50)              # room for the footer

    more = True
    while more:
        device = writer.begin_page(page)
        more, _filled = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()

    _number_the_pages(path, page)
    return path


def _number_the_pages(path: Path, page) -> None:
    """Stamp "page n of m" along the bottom.

    Story lays out flowing content and knows nothing about furniture, so the
    footer goes on afterwards. Worth the second pass: a printed exception
    report gets separated, and a page with no number cannot be put back.
    """
    import pymupdf

    doc = pymupdf.open(path)
    total = doc.page_count
    for n, sheet in enumerate(doc, start=1):
        sheet.insert_text(
            (36, page.height - 26), f"WeldAudit  ·  page {n} of {total}",
            fontname="helv", fontsize=7.5, color=(0.42, 0.45, 0.50))
    doc.save(str(path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    doc.close()
