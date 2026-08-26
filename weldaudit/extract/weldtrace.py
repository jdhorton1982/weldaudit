"""Loading a WeldTrace download into the tables the audit already reads.

The point of this module is what it *does not* create.  A WeldTrace download
is not a twelfth kind of audit; it is a weld register that arrives typed
instead of scanned, so its welds go in ``weld``, its welders fall out of that
into ``welder_pass`` exactly as a daily report's do, its heats go into
``installed_heat`` and ``material``, and every rule and every tab that already
knows how to read those tables gets a WeldTrace job for free.

What those shared tables cannot hold gets a table of its own.  Half of what a
WeldTrace row says - which of eight methods were requested, which report each
returned, whether a failure was retested, which test pack the weld belongs to
and under what reference that pack was issued - has no column in ``weld`` and
never should have: those are questions about one export format, not about
welds in general.  ``weldtrace_weld``, ``weldtrace_exam`` and
``weldtrace_heat`` carry them, and ``weldtrace_stamp`` carries the as-built.

Everything :mod:`weldaudit.rules.weldtrace` needs is written here, including
which stamps matched which weld, so that a rule change re-runs in seconds
without opening an export again and so that the register-against-drawings
match has exactly one implementation.

Two things are deliberately left undone.

The attached PDFs - the signed drawing set and the QAQC-FRM-4347 test plan -
are indexed and left alone.  Storing them is what the package needs; rewriting
a signed PDF would break the seal that makes it worth storing.

The NDE report references are written to ``weld.nde_report`` and not to
``weld.nde_id``.  ``NX-20260331RT01`` is not an NdeId - it has no series and
no sequence - and forcing it into that column would have the gap-in-sequence
and malformed-ticket rules reasoning about a numbering scheme they were not
written for.  :mod:`weldaudit.rules.weldtrace` checks these references against
the test pack instead, which is the comparison this format actually supports.
"""

from __future__ import annotations

from pathlib import Path

from ..db import Database
from ..mtrname import normalise_heat
from ..weldtrace import (
    NDE_METHODS, WeldRow, index_stamps, match_stamps, parse_annotation_csv,
    parse_annotation_pdf, parse_material_register, parse_weld_register,
)

#: What ``weld.source`` says for a weld read out of a WeldTrace register, and
#: the string :data:`weldaudit.rules.registers.REGISTER_OF` names.
SOURCE = "weldtrace"

#: The three exports an audit can actually read, and what each one is for.
#: :func:`weldaudit.rules.weldtrace.download_incomplete` reports the ones a
#: download arrived without.
PARSED_KINDS: tuple[tuple[str, str], ...] = (
    ("weldtrace_welds", "the weld register (TestPackExport.csv)"),
    ("weldtrace_materials", "the heat register (projectMaterialsExport.csv)"),
    ("weldtrace_stamps", "the as-built stamps (AnnotationAttachments_*.pdf)"),
)


def _segment_of(weld: WeldRow, fallback: str) -> str:
    """Which segment a WeldTrace weld belongs to.

    A test pack is the unit the field walks down and signs, so it is the
    segment when the register names one.  Where it does not - a welds export
    often leaves the column blank - the folder the download was unpacked into
    is the honest answer, and never ``(unassigned)``.
    """
    return weld.test_pack or fallback


def _documents(db: Database, project_id: int, kind: str) -> list:
    return db.q(
        "SELECT id, path, filename, segment, fingerprint FROM document "
        "WHERE project_id=? AND kind=? ORDER BY filename",
        (project_id, kind),
    )


def _fallback_segment(document, project_root_name: str) -> str:
    segment = document["segment"]
    if segment in ("", "(unassigned)", None):
        return project_root_name or Path(document["filename"]).stem[:48]
    return segment


def _joined(values) -> str:
    """The distinct values, in the order first seen, ``'; '`` joined."""
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return "; ".join(out)


def extract(db: Database, project_id: int) -> dict[str, int]:
    """Read every WeldTrace export in the project.

    Returns the counts the run summary reports: files read, welds, heats and
    as-built stamps.
    """
    counts = {"weldtrace_files": 0, "weldtrace_welds": 0,
              "weldtrace_heats": 0, "weldtrace_stamps": 0}

    weld_docs = _documents(db, project_id, "weldtrace_welds")
    heat_docs = _documents(db, project_id, "weldtrace_materials")
    stamp_docs = _documents(db, project_id, "weldtrace_stamps")
    if not (weld_docs or heat_docs or stamp_docs):
        return counts

    project = db.one("SELECT root FROM project WHERE id=?", (project_id,))
    root_name = Path(project["root"]).name if project else ""

    # -- the drawings ------------------------------------------------------
    # Read first, because a weld is written with the stamps that match it.
    found: list[tuple] = []          # (stamp, document, segment, source)
    for doc in stamp_docs:
        is_csv = doc["filename"].lower().endswith(".csv")
        try:
            stamps = (parse_annotation_csv(doc["path"]) if is_csv
                      else parse_annotation_pdf(doc["path"]))
        except (OSError, ValueError, RuntimeError):
            continue          # a OneDrive placeholder, or an image-only export
        if not stamps:
            continue
        counts["weldtrace_files"] += 1
        segment = _fallback_segment(doc, root_name)
        source = "weldtrace_csv" if is_csv else "weldtrace_pdf"
        found.extend((stamp, doc, segment, source) for stamp in stamps)
    by_tag = index_stamps([s for s, *_ in found])

    # -- the heat register -------------------------------------------------
    heats: list[tuple] = []          # (heat row, document, segment)
    for doc in heat_docs:
        try:
            register = parse_material_register(doc["path"])
        except OSError:
            continue
        if not register:
            continue
        counts["weldtrace_files"] += 1
        segment = _fallback_segment(doc, root_name)
        heats.extend((material, doc, segment) for material in register.values())

    # -- the weld register -------------------------------------------------
    welds: list[tuple] = []          # (weld, document, segment, its stamps)
    #: Which register weld each stamp was matched to, so that the stamps left
    #: over are the orphans WT-08 reports.  Keyed by identity: two welds can
    #: reach the same stamp through different readings of a tag, and the first
    #: to claim it is the exact match rather than the loose one.
    claimed: dict[int, str] = {}
    for doc in weld_docs:
        try:
            register = parse_weld_register(doc["path"])
        except OSError:
            continue
        if not register:
            continue
        counts["weldtrace_files"] += 1
        fallback = _fallback_segment(doc, root_name)
        for weld in register:
            stamped = match_stamps(weld, by_tag)
            for stamp in stamped:
                claimed.setdefault(id(stamp), weld.weld_number)
            welds.append((weld, doc, _segment_of(weld, fallback), stamped))

    counts["weldtrace_welds"] = len(welds)
    counts["weldtrace_heats"] = len(heats)
    counts["weldtrace_stamps"] = len(found)

    with db.tx() as c:
        c.execute("DELETE FROM weld WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        c.execute("DELETE FROM installed_heat WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        c.execute("DELETE FROM material WHERE project_id=? AND source=?",
                  (project_id, SOURCE))
        for table in ("weldtrace_exam", "weldtrace_weld", "weldtrace_heat",
                      "weldtrace_stamp"):
            c.execute("DELETE FROM " + table + " WHERE project_id=?", (project_id,))

        for material, doc, segment in heats:
            c.execute(
                """INSERT INTO material
                   (project_id, document_id, segment, heat, heat_key,
                    manufacturer, grade, spec, description, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (project_id, doc["id"], segment, material.heat,
                 normalise_heat(material.heat), material.supplier,
                 material.grade, material.spec_no,
                 material.product_form or material.fitting_type or material.name,
                 SOURCE))
            c.execute(
                """INSERT INTO weldtrace_heat
                   (project_id, document_id, segment, heat, heat_key,
                    material_name, product_form, fitting_type, supplier,
                    spec_no, grade, p_no, mtr_file, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, doc["id"], segment, material.heat,
                 normalise_heat(material.heat), material.name,
                 material.product_form, material.fitting_type,
                 material.supplier, material.spec_no, material.grade,
                 material.p_no, material.mtr_file, material.status))

        for weld, doc, segment, stamped in welds:
            # The examination that carries a report is the one worth recording
            # against the joint; VI is a sign-off every weld has and would
            # otherwise mask the RT or PT that actually examined it.
            reported = next((e for e in weld.examined if e.report), None)
            exam = reported or (weld.examined[0] if weld.examined else None)
            cur = c.execute(
                """INSERT INTO weld
                   (project_id, document_id, segment, line, weld_no, weld_size,
                    weld_type, wps, wps_revision, welder_root, welder_fill,
                    welder_cap, date_welded, heat_us, heat_ds, nde_id,
                    nde_report, nde_date, nde_technique, nde_status, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, doc["id"], segment, weld.line or weld.drawing,
                 weld.weld_number, weld.size, weld.joint_type,
                 weld.wps, weld.wps_revision,
                 "; ".join(weld.welders["root"]),
                 "; ".join(weld.welders["fill"]),
                 "; ".join(weld.welders["cap"]),
                 weld.date_welded, weld.heats[0], weld.heats[1],
                 "",                                  # nde_id: see the docstring
                 exam.report if exam else "",
                 exam.date if exam else "",
                 exam.method if exam else "",
                 exam.verdict if exam else "",
                 SOURCE))
            weld_id = cur.lastrowid

            cur = c.execute(
                """INSERT INTO weldtrace_weld
                   (project_id, document_id, weld_id, segment, test_pack,
                    pack_reference, weld_no, weld_tag, drawing, revision,
                    category, joint_type, weld_size, line, line_class, wps,
                    wps_revision, date_planned, date_welded, material_1,
                    heat_1, material_2, heat_2, welders, passes_unmanned,
                    test_result, penalty, stamps, stamped_on)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, doc["id"], weld_id, segment, weld.test_pack,
                 weld.pack_reference, weld.weld_number,
                 str(weld.tag) if weld.tag else "", weld.drawing, weld.revision,
                 weld.category, weld.joint_type, weld.size, weld.line,
                 weld.line_class, weld.wps, weld.wps_revision,
                 weld.date_planned, weld.date_welded,
                 weld.materials[0], weld.heats[0],
                 weld.materials[1], weld.heats[1],
                 _joined(i for ids in weld.welders.values() for i in ids),
                 "; ".join(p for p in ("root", "fill", "cap")
                           if not weld.welders[p]),
                 weld.result, weld.penalty,
                 len(stamped), _joined(s.drawing for s in stamped)))
            wt_weld_id = cur.lastrowid

            c.executemany(
                """INSERT INTO weldtrace_exam
                   (project_id, weldtrace_weld_id, method, requested, verdict,
                    report, report_rev, exam_date, retest_requested,
                    retest_verdict)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(project_id, wt_weld_id, method, e.requested, e.verdict,
                  e.report, e.revision, e.date, e.retest_requested,
                  e.retest_verdict)
                 for method, e in ((m, weld.exams[m]) for m in NDE_METHODS)])

            for heat in weld.heats:
                if heat:
                    c.execute(
                        """INSERT INTO installed_heat
                           (project_id, document_id, segment, line, drawing_no,
                            heat, heat_key, source)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (project_id, doc["id"], segment, weld.line,
                         weld.drawing, heat, normalise_heat(heat), SOURCE))

        c.executemany(
            """INSERT INTO weldtrace_stamp
               (project_id, document_id, segment, drawing, revision, weld_tag,
                raw_tag, welder, stamp_date, page_no, source, matched_weld_no)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(project_id, doc["id"], segment, stamp.drawing, stamp.revision,
              str(stamp.tag), stamp.raw_tag, stamp.welder, stamp.date,
              stamp.page_no, source, claimed.get(id(stamp), ""))
             for stamp, doc, segment, source in found])
    return counts
