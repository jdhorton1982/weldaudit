"""Reconciling the release for backfill against the work it releases.

The release for backfill is the last hold point on a buried line. Once the
ditch is closed, every weld, every coating holiday and every wrong heat under
it stops being an inspection question and becomes an excavation. The form is
one page and it makes three assertions in a single printed sentence:

    "...has had all weld and heat map data captured, all NDE is cleared and
    AC mitigation, if applicable, has been installed and inspected."

Two of those three are checkable against records this tool already holds, and
that is what the rules here do. A weld dated after the release was signed was
not captured by it; an NDE shot dated after it was not cleared by it.

**The join is the segment and the date, not the station.** The form states the
extent it covers as a survey station range — `130+00 to 135+00` — and nothing
else in the corpus places a weld against a station. Rather than invent that
mapping, a segment is treated as released on the date of the *last* release
filed for it, and only welds dated after that are reported. That is sound so
long as every release has been read, and badly wrong otherwise: Bluewater files
27 forms in one PDF, and reading page 1 alone would make July look like the
end of the job. ``fully_read`` is the guard — the date rules skip any segment
whose bundle has not been read through.

What this still cannot see is a weld *inside* the dates but *outside* every
released length. Catching that needs stations on welds, which no document in
this corpus provides.

**"Signed" means the earliest signature**, because the ditch could be closed
from that moment. Bluewater's 7-25-25 release was counter-signed by the
contractor on 9-6-25, six weeks later; taking the latest date would let a late
counter-signature retrospectively excuse anything done in between.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from ..db import Database
from . import Finding, register

#: Signature dates on one release differing by more than this are worth
#: reporting. A day or two is two people signing the same form in sequence.
SIGNATURE_DRIFT_DAYS = 7


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def _as_date(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _releases(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT b.*, d.filename FROM backfill_release b
           LEFT JOIN document d ON d.id = b.document_id
           WHERE b.project_id=? ORDER BY b.segment, b.released_on, b.page_no""",
        (project_id,),
    )


def fully_read(db: Database, project_id: int) -> set[str]:
    """Segments whose release bundles have been read end to end.

    A segment is released in lengths — Bluewater files 27 forms in one PDF — and
    "the last release" only means the last one if every page has been read.
    Read page 1 of 27 and the other 26 releases are invisible, which would
    report every weld made after July as buried without a hold point. So the
    date-based rules only run on a bundle that was read end to end.

    **Measured by pages read, not by releases found.** Counting the releases
    would mean a bundle containing anything that is not a release — a cover
    sheet, a divider, a page the model declined — could never satisfy the
    guard, and AB-01, BF-01 and BF-02 would stay silent after a complete and
    correct pass with nothing to say why. Whether the reading finished is a
    question about the reading.
    """
    from ..vision import page_count

    complete: set[str] = set()
    for r in db.q(
        """SELECT id, path, segment, fingerprint FROM document
           WHERE project_id=? AND kind='backfill' AND ext IN ('.pdf','.PDF')""",
        (project_id,),
    ):
        # page_count returns 0 for a PDF it cannot open. That must not read as
        # "zero pages, therefore fully read" — an unreadable bundle is exactly
        # the case where we do not know what else it contains.
        pages = page_count(r["path"])
        if not pages:
            continue
        fingerprint = r["fingerprint"] or str(r["id"])
        read = sum(
            1 for page_no in range(pages)
            if _was_read(db, fingerprint, page_no)
        )
        if read >= pages:
            complete.add(r["segment"] or "")
    return complete


def _was_read(db: Database, fingerprint: str, page_no: int) -> bool:
    """Whether a page has a usable cached reading, at any model or resolution.

    A refusal or an unparsable reply is not a reading — those are the pages
    most likely to hold something the rest of the bundle does not.
    """
    payload = db.ocr_any(fingerprint, "backfill", page_no)
    return payload is not None and not payload.get("_error")


def _by_segment(db: Database, project_id: int) -> dict[str, dict]:
    """The last release per segment, and how many cover it.

    A segment is released in lengths, one form per stretch of ditch. The
    latest release is when the last of it was closed, and a weld dated after
    that is after everything.
    """
    out: dict[str, dict] = {}
    for r in _releases(db, project_id):
        if not r["segment"] or not r["released_on"]:
            continue
        entry = out.setdefault(r["segment"], {
            "segment": r["segment"], "releases": 0, "last": "", "first": "",
            "document_id": r["document_id"], "page_no": r["page_no"],
            "stations": [],
        })
        entry["releases"] += 1
        if r["released_on"] > entry["last"]:
            entry["last"] = r["released_on"]
            entry["document_id"] = r["document_id"]
            entry["page_no"] = r["page_no"]
        if not entry["first"] or r["released_on"] < entry["first"]:
            entry["first"] = r["released_on"]
        if r["from_station"] and r["to_station"]:
            entry["stations"].append(f"{r['from_station']}–{r['to_station']}")
    return out


def _label(entry: dict) -> str:
    n = entry["releases"]
    return (f"the {entry['last']} release" if n == 1
            else f"the last of {n} releases, on {entry['last']}")


# ---------------------------------------------------------------------------


@register("BF-01", "Weld made after the ditch was released for backfill")
def weld_after_release(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A weld dated after the release that says all weld data was captured.

    Either the weld is not covered by any release — pipe buried without the
    hold point — or the release was signed for work that had not happened.
    """
    complete = fully_read(db, project_id)
    findings: list[Finding] = []
    for segment, entry in sorted(_by_segment(db, project_id).items()):
        if segment not in complete:
            continue
        later = db.q(
            """SELECT weld_no, nde_id, date_welded, document_id FROM weld
               WHERE project_id=? AND segment=? AND date_welded<>''
                 AND date_welded > ?
               ORDER BY date_welded, weld_no""",
            (project_id, segment, entry["last"]),
        )
        if not later:
            continue
        names = [w["nde_id"] or w["weld_no"] for w in later]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-01",
            "severity": "critical", "segment": segment,
            "subject": f"{len(later)} welds",
            "message": (
                f"{len(later)} weld{'s are' if len(later) != 1 else ' is'} dated "
                f"after {_label(entry)} for this segment — "
                f"{', '.join(names[:8])}{'...' if len(names) > 8 else ''}, the "
                f"last on {later[-1]['date_welded']}. The release states that "
                f"all weld and heat map data was captured and all NDE cleared, "
                f"which cannot be true of a joint made afterwards."
            ),
            "detail": _detail(released=entry["last"], releases=entry["releases"],
                              welds=", ".join(names[:40]),
                              last_welded=later[-1]["date_welded"]),
            "document_id": entry["document_id"], "page_no": entry["page_no"],
        })
    return findings


@register("BF-02", "NDE shot after the ditch was released for backfill")
def nde_after_release(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A reader sheet dated after the release that says NDE was cleared.

    Less severe than a late weld — the shot may be a re-read of film already
    taken — but the form's assertion was untrue when it was signed either way.
    """
    complete = fully_read(db, project_id)
    findings: list[Finding] = []
    for segment, entry in sorted(_by_segment(db, project_id).items()):
        if segment not in complete:
            continue
        later = db.q(
            """SELECT DISTINCT nde_id, sheet_date FROM nde_shot
               WHERE project_id=? AND segment=? AND IFNULL(sheet_date,'')<>''
                 AND sheet_date > ?
               ORDER BY sheet_date, nde_id""",
            (project_id, segment, entry["last"]),
        )
        if not later:
            continue
        names = [r["nde_id"] for r in later]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-02",
            "severity": "major", "segment": segment,
            "subject": f"{len(later)} shots",
            "message": (
                f"{len(later)} NDE shot{'s are' if len(later) != 1 else ' is'} "
                f"dated after {_label(entry)} for this segment — "
                f"{', '.join(names[:8])}{'...' if len(names) > 8 else ''}, the "
                f"last on {later[-1]['sheet_date']}. \"All NDE is cleared\" was "
                f"not true of this segment on the day it was signed."
            ),
            "detail": _detail(released=entry["last"], shots=", ".join(names[:40]),
                              last_shot=later[-1]["sheet_date"]),
            "document_id": entry["document_id"], "page_no": entry["page_no"],
        })
    return findings


@register("BF-03", "Ditch released with an unresolved rejected weld")
def released_with_open_reject(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A release signed on a segment carrying a reject with no repair.

    NDE-05 already reports the unresolved reject. This is the separate and
    worse fact that the ditch was then closed over it: the same defect, no
    longer reachable without excavating.
    """
    findings: list[Finding] = []
    for segment, entry in sorted(_by_segment(db, project_id).items()):
        rejects = db.q(
            """SELECT weld_no, nde_id, nde_status, defect FROM weld
               WHERE project_id=? AND segment=?
                 AND (nde_status LIKE '%eject%' OR nde_status LIKE '%ail%')
                 AND IFNULL(repair_nde_id,'')=''
               ORDER BY nde_id, weld_no""",
            (project_id, segment),
        )
        if not rejects:
            continue
        names = [r["nde_id"] or r["weld_no"] for r in rejects]
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-03",
            "severity": "critical", "segment": segment,
            "subject": f"{len(rejects)} welds",
            "message": (
                f"This segment was released for backfill on {entry['last']} "
                f"with {len(rejects)} rejected weld"
                f"{'s' if len(rejects) != 1 else ''} still showing no repair — "
                f"{', '.join(names[:8])}{'...' if len(names) > 8 else ''}. The "
                f"release asserts all NDE is cleared, and the ditch is now "
                f"closed over the defect."
            ),
            "detail": _detail(released=entry["last"], welds=", ".join(names[:40])),
            "document_id": entry["document_id"], "page_no": entry["page_no"],
        })
    return findings


@register("BF-04", "Release for backfill is not fully signed")
def release_unsigned(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A release missing a signature or a date.

    The form is two or three signatures and nothing else; an unsigned one
    releases nothing, whatever was written above it.
    """
    findings: list[Finding] = []
    for r in _releases(db, project_id):
        missing = []
        if not r["inspector_signed"]:
            missing.append("the inspector's signature")
        elif not r["inspector_date"]:
            missing.append("the inspector's date")
        if not r["contractor_signed"]:
            missing.append("the contractor's signature")
        elif not r["contractor_date"]:
            missing.append("the contractor's date")
        # A missing survey line is a form revision, not an omission — only a
        # signed survey line with no date against it counts.
        if r["survey_signed"] and not r["survey_date"]:
            missing.append("the survey rep's date")
        if not missing:
            continue

        where = f"{r['from_station']} to {r['to_station']}" if r["from_station"] else ""
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-04",
            "severity": "major", "segment": r["segment"],
            "subject": where or f"page {r['page_no']}",
            "message": (
                f"The release for backfill on page {r['page_no']} of "
                f"{r['filename']}"
                + (f", covering {where}," if where else "")
                + f" is missing {' and '.join(missing)}. Nothing on it "
                  f"authorises the ditch to be closed."
            ),
            "detail": _detail(page=r["page_no"], missing=", ".join(missing),
                              from_station=r["from_station"],
                              to_station=r["to_station"]),
            "document_id": r["document_id"], "page_no": r["page_no"],
        })
    return findings


@register("BF-05", "Signatures on one release are weeks apart")
def signature_drift(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Signature dates on one form separated by more than a week.

    Bluewater has a release the inspector and surveyor signed on 7-25-25 and the
    contractor on 9-6-25. Either the ditch stood open for six weeks or one
    signature was added after the fact; both matter, because the release date
    is what every other date on the job is measured against.
    """
    findings: list[Finding] = []
    for r in _releases(db, project_id):
        dated = {
            "inspector": _as_date(r["inspector_date"]),
            "contractor": _as_date(r["contractor_date"]),
            "survey rep": _as_date(r["survey_date"]),
        }
        present = {who: when for who, when in dated.items() if when}
        if len(present) < 2:
            continue
        first = min(present.values())
        last = max(present.values())
        gap = (last - first).days
        if gap <= SIGNATURE_DRIFT_DAYS:
            continue
        earliest = ", ".join(sorted(w for w, d in present.items() if d == first))
        latest = ", ".join(sorted(w for w, d in present.items() if d == last))
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-05",
            "severity": "major", "segment": r["segment"],
            "subject": f"{gap} days",
            "message": (
                f"On the release on page {r['page_no']} of {r['filename']}, "
                f"{earliest} signed on {first.isoformat()} and {latest} on "
                f"{last.isoformat()} — {gap} days apart. Either the ditch stood "
                f"open that long or a signature was added afterwards; the audit "
                f"treats the earliest as the release date, so the difference "
                f"changes what counts as late."
            ),
            "detail": _detail(page=r["page_no"], gap_days=gap,
                              **{w: d.isoformat() for w, d in present.items()}),
            "document_id": r["document_id"], "page_no": r["page_no"],
        })
    return findings


@register("BF-06", "Segment welded with no release for backfill")
def segment_unreleased(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A welded segment with no release form, where other segments have one.

    Guarded on the job having at least one: with none read, every segment
    would fire and the finding would carry no information.
    """
    released = set(_by_segment(db, project_id))
    if not released:
        return []

    findings: list[Finding] = []
    for row in db.q(
        """SELECT segment, COUNT(*) n FROM weld
           WHERE project_id=? AND segment<>'' GROUP BY segment ORDER BY segment""",
        (project_id,),
    ):
        if row["segment"] in released:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-06",
            "severity": "major", "segment": row["segment"],
            "subject": row["segment"],
            "message": (
                f"{row['n']} weld{'s are' if row['n'] != 1 else ' is'} recorded "
                f"on this segment and no release for backfill is filed against "
                f"it, while other segments on this job have one. Nothing "
                f"records the hold point being cleared before the ditch closed."
            ),
            "detail": _detail(welds=row["n"],
                              released_segments=", ".join(sorted(released))),
            "document_id": None, "page_no": None,
        })
    return findings


@register("BF-07", "Release for backfill states no extent")
def release_without_extent(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A release with one or both survey stations blank.

    The stations are the only thing on the form saying *what* was released.
    Without them the release covers an unstated length of ditch, and nothing
    downstream can tell whether a given joint was inside it.
    """
    blank = [r for r in _releases(db, project_id)
             if not r["from_station"] or not r["to_station"]]
    if not blank:
        return []

    by_document: dict[tuple, list] = defaultdict(list)
    for r in blank:
        by_document[(r["document_id"], r["filename"], r["segment"])].append(r)

    findings: list[Finding] = []
    for (document_id, filename, segment), rows in by_document.items():
        pages = ", ".join(str(r["page_no"]) for r in rows[:12])
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "BF-07",
            "severity": "minor", "segment": segment,
            "subject": f"{len(rows)} releases",
            "message": (
                f"{len(rows)} release{'s in' if len(rows) != 1 else ' in'} "
                f"{filename} state no survey station range (page{'s' if len(rows) != 1 else ''} "
                f"{pages}). The stations are the only thing on the form saying "
                f"what length of ditch was released."
            ),
            "detail": _detail(pages=pages, releases=len(rows)),
            "document_id": document_id, "page_no": rows[0]["page_no"],
        })
    return findings


def release_summary(db: Database, project_id: int) -> list[dict]:
    """Every release, with what it covers and who signed it."""
    out: list[dict] = []
    for r in _releases(db, project_id):
        signed = [w for w, ok in (("inspector", r["inspector_signed"]),
                                  ("contractor", r["contractor_signed"]),
                                  ("survey", r["survey_signed"])) if ok]
        out.append({
            "segment": r["segment"] or "",
            "page_no": r["page_no"],
            "line_size": r["line_size"] or "",
            "service": r["service"] or "",
            "from_station": r["from_station"] or "",
            "to_station": r["to_station"] or "",
            "released_on": r["released_on"] or "",
            "inspector_date": r["inspector_date"] or "",
            "contractor_date": r["contractor_date"] or "",
            "survey_date": r["survey_date"] or "",
            "signed_by": ", ".join(signed),
            "document_id": r["document_id"],
            "filename": r["filename"] or "",
        })
    return out
