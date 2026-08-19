"""Reconciling the hydrostatic pressure test against the rest of the package.

A pressure test is the one document in the book that proves the line as built
actually holds.  Everything else certifies a part or a joint; this certifies
the assembly.  That makes it worth checking hard, and it is checkable, because
the package states its own pass criteria: a requirements sheet gives the
minimum and maximum test pressure and the required duration, and the record
gives what was actually held, minute by minute, with two signatures under it.

Four independent things can be wrong, and the rules here separate them:

* the test did not meet its own stated requirement (HYD-01..HYD-03);
* the paperwork does not say whether it passed (HYD-04);
* the instruments that measured it were not in calibration (HYD-05);
* the test does not cover the work — a weld was made after it, or a line was
  never tested at all (HYD-06, HYD-07).

The last is the one hand auditing misses most easily, because it needs the
weld dates and the test date side by side and they live in different sections
of the book.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..db import Database
from . import Finding, register

#: The Marden pressure test plan states the rule the industry works to:
#: gauges and recorders calibrated within six months of the test.
CALIBRATION_MONTHS = 6
CALIBRATION_DAYS = 183

#: How far short of the required hold is worth reporting.  Forms are written
#: to the nearest five minutes, so an exact comparison would fire on rounding.
DURATION_SLACK_HOURS = 0.25

#: How long the readings may stop before the recorded completion time without
#: it being worth a note.
READING_GAP_HOURS = 0.5


def _detail(**kw) -> str:
    return json.dumps({k: v for k, v in kw.items() if v not in (None, "")})


def every_package_read(db: Database, project_id: int) -> bool:
    """Whether every pressure test package has been read end to end.

    The third rule in this codebase to need such a guard, after the backfill
    releases and the coating reports, and for the same reason each time: a
    rule that reasons from *absence* is only sound once the reading is
    finished. Counted by pages read rather than by tests produced, so that a
    package's charts and calibration certificates — which are most of it — do
    not make it permanently unread.
    """
    from ..vision import page_count

    for r in db.q(
        """SELECT MIN(id) id, path, fingerprint FROM document
           WHERE project_id=? AND kind='hydrotest' AND ext IN ('.pdf','.PDF')
           GROUP BY IFNULL(fingerprint, id)""",
        (project_id,),
    ):
        fingerprint = r["fingerprint"] or str(r["id"])
        pages = page_count(r["path"])
        if not pages:
            return False           # cannot open it, so cannot know
        read = [db.ocr_any(fingerprint, "hydrotest", n) for n in range(pages)]
        if not any(p is not None for p in read):
            continue               # not a package this pass targeted at all
        # Started but not finished is the dangerous state, and the record can
        # be on any page — so a package read in part is a package not read.
        if not all(p is not None and not p.get("_error") for p in read):
            return False
    return True


def _tests(db: Database, project_id: int) -> list:
    return db.q(
        """SELECT h.*, d.filename FROM hydrotest h
           LEFT JOIN document d ON d.id = h.document_id
           WHERE h.project_id=? ORDER BY h.segment, h.id""",
        (project_id,),
    )


def _readings(db: Database, hydrotest_id: int) -> list:
    return db.q(
        """SELECT seq, reading_time, pressure FROM hydrotest_reading
           WHERE hydrotest_id=? ORDER BY seq""",
        (hydrotest_id,),
    )


def _label(test) -> str:
    """How to name a test in a message: its service, else its file."""
    return (test["service"] or test["line_no"] or test["segment"]
            or test["filename"] or "the pressure test")


def _when(test) -> str:
    return test["started_raw"] or test["started_at"] or ""


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _clock(text: str) -> timedelta | None:
    """Minutes past midnight from a reading time such as '1:25 PM'."""
    import re

    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})\s*([ap])", str(text or ""), re.IGNORECASE)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hour > 12 or minute > 59:
        return None
    if hour == 12:
        hour = 0
    if half == "p":
        hour += 12
    return timedelta(hours=hour, minutes=minute)


def hold_window(readings: list, floor: float | None) -> tuple[int, int, float] | None:
    """``(first index at pressure, last index, hours held)`` for the hold.

    The readings table opens with the pressurisation ramp — on Kestrel 8 it runs
    109, 219, 326 psig before reaching test pressure — and those are not part
    of the hold.  The hold starts at the first reading that reaches the
    required minimum, so ramp readings are never mistaken for a pressure drop.
    """
    timed = [(i, _clock(r["reading_time"]), r["pressure"]) for i, r in enumerate(readings)]
    timed = [(i, t, p) for i, t, p in timed if t is not None]
    if len(timed) < 2:
        return None

    start = 0
    if floor is not None:
        at_pressure = [n for n, (_i, _t, p) in enumerate(timed)
                       if p is not None and p >= floor]
        if not at_pressure:
            return None
        start = at_pressure[0]

    # Times carry no date. A test that runs past midnight steps backwards, so
    # a decreasing clock means the next day rather than a transcription error.
    elapsed = timedelta()
    previous = timed[start][1]
    for _i, moment, _p in timed[start + 1:]:
        step = moment - previous
        if step < timedelta():
            step += timedelta(days=1)
        elapsed += step
        previous = moment
    return timed[start][0], timed[-1][0], elapsed.total_seconds() / 3600


def test_summary(db: Database, project_id: int) -> list[dict]:
    """Every pressure test on the job, required against actual.

    That comparison is the whole audit of a pressure test, so the two sit
    side by side rather than in separate views.
    """
    out: list[dict] = []
    for test in db.q(
        """SELECT h.*, d.path AS doc_path, d.filename FROM hydrotest h
           LEFT JOIN document d ON d.id = h.document_id
           WHERE h.project_id=? ORDER BY h.segment, h.started_at""",
        (project_id,),
    ):
        readings = _readings(db, test["id"])
        held = [r["pressure"] for r in readings if r["pressure"] is not None]
        # Exclude the pressurisation ramp, so "low" means the hold rather than
        # the pressure on the way up.
        if test["req_min_press"] is not None:
            held = [p for p in held if p >= test["req_min_press"]] or held
        hours, basis = _held_hours(db, test)
        out.append({
            "segment": test["segment"] or "", "service": test["service"] or "",
            "code": test["code"] or "",
            "started": test["started_raw"] or (test["started_at"] or "")[:16],
            "completed": test["completed_raw"] or (test["completed_at"] or "")[:16],
            "required_min": test["req_min_press"],
            "required_max": test["req_max_press"],
            "held_low": min(held) if held else None,
            "held_high": max(held) if held else None,
            "required_hours": test["req_hours"],
            "actual_hours": round(hours, 1) if hours is not None else None,
            "duration_basis": basis,
            # A read record with no box ticked is a finding, not a blank: the
            # two must never look the same in a summary.
            "result": test["result"] or ("(unmarked)" if test["page_no"] else ""),
            "medium": test["medium"] or "", "readings": len(readings),
            "inspector": test["inspector"] or "",
            "document_id": test["document_id"], "filename": test["filename"] or "",
        })
    return out


# ---------------------------------------------------------------------------


@register("HYD-01", "Test pressure below the required minimum")
def pressure_below_minimum(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The pressure the record claims to have held is under the requirement.

    This is the test failing on its own face: the two numbers are written on
    the same form, and one of them does not meet the other.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        required = test["req_min_press"]
        held = _lowest_hold_pressure(db, test)
        if required is None or held is None or held >= required:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-01",
            "severity": "critical", "segment": test["segment"],
            "subject": _label(test),
            "message": (
                f"The pressure test of {_label(test)} was required to hold at "
                f"least {required:g} psig, but the lowest reading during the "
                f"hold was {held:g} psig. The line was not tested to the "
                f"pressure its own test sheet requires."
            ),
            "detail": _detail(required_minimum=required, lowest_reading=held,
                              started=_when(test)),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings


def _lowest_hold_pressure(db: Database, test) -> float | None:
    readings = _readings(db, test["id"])
    window = hold_window(readings, test["req_min_press"])
    if window:
        first, last, _hours = window
        held = [r["pressure"] for r in readings[first:last + 1] if r["pressure"] is not None]
        if held:
            return min(held)
    # No usable readings: fall back to the minimum the form states it held.
    return None


@register("HYD-02", "Test pressure exceeded the stated maximum")
def pressure_above_maximum(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A reading above the maximum test pressure the sheet allows.

    The ceiling exists because the test pressure is a fixed multiple of the
    design pressure; overshooting it can yield the pipe rather than prove it.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        ceiling = test["req_max_press"]
        if ceiling is None:
            continue
        readings = _readings(db, test["id"])
        over = [r for r in readings if r["pressure"] is not None and r["pressure"] > ceiling]
        if not over:
            continue
        peak = max(r["pressure"] for r in over)
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-02",
            "severity": "major", "segment": test["segment"],
            "subject": _label(test),
            "message": (
                f"The pressure test of {_label(test)} reached {peak:g} psig "
                f"against a stated maximum of {ceiling:g} psig"
                f"{_at(over[0])}. Overshooting the maximum can yield the pipe "
                f"instead of proving it, and needs an engineering disposition."
            ),
            "detail": _detail(maximum=ceiling, peak=peak,
                              readings_over=len(over), started=_when(test)),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings


def _at(reading) -> str:
    return f", first at {reading['reading_time']}" if reading["reading_time"] else ""


@register("HYD-03", "Test held for less than the required duration")
def duration_short(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The hold was shorter than the sheet requires.

    Measured from the record's own start and finish times where it gives them,
    because those are what the signatures attest to; the readings table is
    used only where the times are missing.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        required = test["req_hours"]
        if required is None:
            continue
        actual, basis = _held_hours(db, test)
        if actual is None or actual >= required - DURATION_SLACK_HOURS:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-03",
            "severity": "critical", "segment": test["segment"],
            "subject": _label(test),
            "message": (
                f"The pressure test of {_label(test)} was required to hold for "
                f"{required:g} hours but {basis} {actual:.1f}. A short hold "
                f"does not demonstrate the line is tight."
            ),
            "detail": _detail(required_hours=required, actual_hours=round(actual, 2),
                              basis=basis, started=_when(test),
                              completed=test["completed_raw"]),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings


def _held_hours(db: Database, test) -> tuple[float | None, str]:
    start, end = _moment(test["started_at"]), _moment(test["completed_at"])
    if start and end and end > start:
        hours = (end - start).total_seconds() / 3600
        return hours, "the record's own start and finish times give"
    if test["stated_hours"] is not None:
        return float(test["stated_hours"]), "the record states"
    window = hold_window(_readings(db, test["id"]), test["req_min_press"])
    if window:
        return window[2], "the logged readings span only"
    return None, ""


@register("HYD-04", "Pressure test result not recorded")
def result_not_recorded(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """Neither result box ticked, or the test recorded as unacceptable.

    An unmarked pair is the common case and the easy one to miss: the form is
    complete, signed, and filed, and never actually says the test passed.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        # A row with no record page read has nothing to say about the result.
        if test["page_no"] is None:
            continue
        result = (test["result"] or "").upper()
        if result == "ACCEPTABLE":
            continue
        if result == "UNACCEPTABLE":
            severity = "critical"
            message = (
                f"The pressure test of {_label(test)} is recorded as "
                f"UNACCEPTABLE. Either a retest is missing from the package or "
                f"the line was accepted on a failed test."
            )
        else:
            severity = "major"
            message = (
                f"The pressure test of {_label(test)} has neither the "
                f"ACCEPTABLE nor the UNACCEPTABLE box marked. The form is "
                f"signed but never states the outcome, so nothing in the "
                f"package says this line passed."
            )
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-04",
            "severity": severity, "segment": test["segment"],
            "subject": _label(test),
            "message": message,
            "detail": _detail(result=result or "(unmarked)",
                              inspector=test["inspector"],
                              contractor=test["contractor_rep"],
                              started=_when(test)),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings


@register("HYD-05", "Test instrument out of calibration")
def instrument_calibration(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A gauge or recorder with no calibration certificate, or a stale one.

    Only runs once the package's calibration certificates have been read: with
    none on file the answer is "not known", not "not calibrated", and firing on
    every instrument would bury the cases where a certificate really is stale.
    """
    certs = {
        r["serial_key"]: r
        for r in db.q(
            "SELECT serial_key, serial, calibrated, document_id FROM instrument_cal "
            "WHERE project_id=? AND serial_key<>''",
            (project_id,),
        )
    }
    if not certs:
        return []

    from ..extract.vision_pass import serial_key

    findings: list[Finding] = []
    for test in _tests(db, project_id):
        started = _moment(test["started_at"])
        for column, what in (("deadweight_sn", "deadweight tester"),
                             ("press_rec_sn", "pressure recorder"),
                             ("temp_rec_sn", "temperature recorder")):
            serial = test[column]
            if not serial:
                continue
            cert = certs.get(serial_key(serial))
            if cert is None:
                findings.append({
                    "project_id": project_id, "run_id": run_id, "rule": "HYD-05",
                    "severity": "major", "segment": test["segment"],
                    "subject": serial,
                    "message": (
                        f"The {what} used on {_label(test)}, serial {serial}, "
                        f"has no calibration certificate in the package. An "
                        f"uncalibrated instrument cannot evidence the hold."
                    ),
                    "detail": _detail(instrument=what, serial=serial,
                                      started=_when(test)),
                    "document_id": test["document_id"], "page_no": test["page_no"],
                })
                continue

            calibrated = _moment(cert["calibrated"])
            if not (started and calibrated):
                continue
            age = (started - calibrated).days
            if age <= CALIBRATION_DAYS:
                continue
            findings.append({
                "project_id": project_id, "run_id": run_id, "rule": "HYD-05",
                "severity": "major", "segment": test["segment"],
                "subject": serial,
                "message": (
                    f"The {what} used on {_label(test)}, serial {serial}, was "
                    f"last calibrated {cert['calibrated']} — {age} days before "
                    f"the test, against the {CALIBRATION_MONTHS}-month limit "
                    f"the test plan sets."
                ),
                "detail": _detail(instrument=what, serial=serial, age_days=age,
                                  calibrated=cert["calibrated"], started=_when(test)),
                "document_id": cert["document_id"], "page_no": None,
            })
    return findings


@register("HYD-06", "Weld made after the line was pressure tested")
def weld_after_test(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A joint welded into a segment after that segment's test.

    Whatever the test proved, it did not prove this weld: the joint did not
    exist when the pressure was on it. Golden welds are exempted by procedure
    and NDE, but the exemption has to be a documented decision rather than an
    accident of sequencing, so it is worth surfacing every time.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        tested = _moment(test["started_at"])
        if not tested or not test["segment"]:
            continue
        later = db.q(
            """SELECT weld_no, nde_id, date_welded, document_id, source
               FROM weld
               WHERE project_id=? AND segment=? AND date_welded<>''
                 AND date_welded > ?
               ORDER BY date_welded, weld_no""",
            (project_id, test["segment"], tested.date().isoformat()),
        )
        if not later:
            continue
        names = [r["nde_id"] or r["weld_no"] for r in later]
        sample = ", ".join(names[:8])
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-06",
            "severity": "critical", "segment": test["segment"],
            "subject": f"{len(later)} welds",
            "message": (
                f"{len(later)} weld{'s' if len(later) != 1 else ''} on this "
                f"segment {'are' if len(later) != 1 else 'is'} dated after the "
                f"pressure test of {_label(test)} on "
                f"{tested.date().isoformat()} ({sample}"
                f"{'...' if len(names) > 8 else ''}, last "
                f"{later[-1]['date_welded']}). "
                f"The test cannot have proved a joint that did not yet exist; "
                f"each needs a golden-weld disposition or a retest."
            ),
            "detail": _detail(tested=tested.date().isoformat(),
                              welds=", ".join(names[:40]),
                              last_welded=later[-1]["date_welded"]),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings


@register("HYD-07", "Segment has welds but no pressure test")
def segment_untested(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """A segment that was welded and has no test record filed against it.

    Reasoning from absence, so it waits until every package has been read
    through. Having *at least one* test is not enough: seeding a single one of
    PLU's twelve packages makes six segments look untested, every one of them
    an artefact of stopping early. A package is 14 to 37 pages and the record
    can be anywhere in it, so "read" means every page.
    """
    tested = {t["segment"] for t in _tests(db, project_id) if t["segment"]}
    if not tested or not every_package_read(db, project_id):
        return []

    findings: list[Finding] = []
    rows = db.q(
        """SELECT segment, COUNT(*) n FROM weld
           WHERE project_id=? AND segment<>'' GROUP BY segment ORDER BY segment""",
        (project_id,),
    )
    for row in rows:
        if row["segment"] in tested:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-07",
            "severity": "major", "segment": row["segment"],
            "subject": row["segment"],
            "message": (
                f"{row['n']} weld{'s' if row['n'] != 1 else ''} "
                f"{'are' if row['n'] != 1 else 'is'} recorded on this segment, "
                f"but no pressure test record is filed against it, while other "
                f"segments on this job have one. Either the test is filed "
                f"elsewhere or the line was never tested."
            ),
            "detail": _detail(welds=row["n"],
                              tested_segments=", ".join(sorted(tested))),
            "document_id": None, "page_no": None,
        })
    return findings


@register("HYD-08", "Readings stop before the test was completed")
def readings_stop_early(db: Database, project_id: int, run_id: str) -> list[Finding]:
    """The logged readings end well before the recorded completion time.

    Not a failed test — the recorder chart covers the gap — but the test log
    on its own then evidences a shorter hold than the form claims, which is
    what an auditor reading only the log would conclude.
    """
    findings: list[Finding] = []
    for test in _tests(db, project_id):
        end = _moment(test["completed_at"])
        readings = _readings(db, test["id"])
        if not end or end.time() == datetime.min.time() or not readings:
            continue
        last = _clock(readings[-1]["reading_time"])
        if last is None:
            continue
        finish = timedelta(hours=end.hour, minutes=end.minute)
        gap = (finish - last).total_seconds() / 3600
        if gap <= READING_GAP_HOURS:
            continue
        findings.append({
            "project_id": project_id, "run_id": run_id, "rule": "HYD-08",
            "severity": "minor", "segment": test["segment"],
            "subject": _label(test),
            "message": (
                f"The test log for {_label(test)} records its last reading at "
                f"{readings[-1]['reading_time']}, {gap:.1f} hours before the "
                f"{test['completed_raw'] or 'recorded'} completion. The "
                f"recorder chart should confirm the pressure held over that "
                f"period; the log on its own does not."
            ),
            "detail": _detail(last_reading=readings[-1]["reading_time"],
                              completed=test["completed_raw"],
                              gap_hours=round(gap, 2)),
            "document_id": test["document_id"], "page_no": test["page_no"],
        })
    return findings
