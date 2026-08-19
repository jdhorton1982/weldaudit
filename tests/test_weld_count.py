"""A reader sheet's own count of the welds it examined.

The Precision Group form prints `Weld Count` at the foot of every report and
is the only form in the corpus that does. Everywhere else, how many welds a
sheet covers has to be inferred from its filename or from the rows that could
be read off it — so this is the one place a sheet can be checked against
itself.

The comparison is deliberately one-sided. A bundle holds several reports and
these pages are scans, so there is no way to know every count was found: the
sum is a *lower bound*. Fewer shots than that is sound. More says nothing.
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.db import Database  # noqa: E402
from weldaudit.readersheet import (  # noqa: E402
    stated_pagination, stated_ticket, stated_weld_count,
)
from weldaudit.rules.nde_coverage import (  # noqa: E402
    malformed_ticket, missing_report_pages, ticket_out_of_order,
    ticket_spans_days, weld_count_short,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "w.db")
    pid = database.upsert_project("W", str(tmp_path))
    with database.tx() as c:
        c.execute(
            """INSERT INTO document(id, project_id, path, filename, ext, kind)
               VALUES(1, ?, 'x.pdf', 'x.pdf', '.pdf', 'nde_reader_sheet')""",
            (pid,),
        )
    return database, pid


def sheet(db, pid, count, *, fp="fp1", page=1,
          filename="FPT-029-032  8-4-25.pdf", evidence="text"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO reader_sheet(project_id, document_id, fingerprint,
                                        filename, segment, page_no, weld_count,
                                        evidence)
               VALUES(?, 1, ?, ?, 'SEG A', ?, ?, ?)""",
            (pid, fp, filename, page, count, evidence),
        )


def shots(db, pid, ids, *, fp="fp1"):
    with db.tx() as c:
        for nde_id in ids:
            c.execute(
                """INSERT INTO nde_shot(project_id, document_id, fingerprint,
                                        nde_id, prefix, number, suffix)
                   VALUES(?, 1, ?, ?, 'FPT', 1, '')""",
                (pid, fp, nde_id),
            )


def run(db, pid):
    return weld_count_short(db, pid, "run")


# -- reading the figure off the page ----------------------------------------

def test_the_count_is_read():
    assert stated_weld_count("Hours Worked: 2.0    Weld Count: 8") == 8
    assert stated_weld_count("Weld Count:37") == 37
    assert stated_weld_count("weld count 5") == 5


def test_a_page_with_no_count_states_none():
    assert stated_weld_count("Hours Worked: 2.0") is None
    assert stated_weld_count("") is None


def test_the_pagination_is_read():
    # The words come out of the text layer split across lines.
    assert stated_pagination("Ticket No: 18700172\nPage: 8\nof 8\n") == (8, 8)
    assert stated_pagination("Page 1 of 3") == (1, 3)


def test_an_unfilled_pagination_is_not_page_one_of_one():
    # 200 of 208 Bluewater sheets print the labels; the text reads `Page:\nof\n`
    # and the crew filled in nothing. Reading that as 1 of 1 would invent a
    # claim the sheet never made.
    assert stated_pagination("Ticket No:\nPage:\nof\nCustomer:") is None


def test_the_ticket_is_read_from_either_form():
    assert stated_ticket("Ticket No: 18700172") == "18700172"
    assert stated_ticket("REF#: RT-1061-0794") == "RT-1061-0794"


def test_a_blank_ticket_box_reads_as_blank():
    # `Ticket No: Page: 3 of 4` — the label with nothing after it. Taking the
    # next number would make the page number into a ticket.
    assert stated_ticket("Ticket No: Page: 3 of 4 RADIOGRAPHIC") == ""
    assert stated_ticket("Ticket No: -------- SERVICES") == ""


# -- the check --------------------------------------------------------------

def test_fewer_shots_than_stated_is_reported(db):
    database, pid = db
    sheet(database, pid, 8)
    shots(database, pid, ["FPT-015", "FPT-016"])
    found = run(database, pid)
    assert len(found) == 1
    assert "states 8 welds examined" in found[0]["message"]
    assert "only 2 shots" in found[0]["message"]


def test_a_sheet_that_adds_up_is_quiet(db):
    database, pid = db
    sheet(database, pid, 4)
    shots(database, pid, ["FPT-029", "FPT-030", "FPT-031", "FPT-032"])
    assert run(database, pid) == []


def test_more_shots_than_stated_says_nothing(db):
    # The sum is a lower bound, so a surplus is not evidence of anything —
    # `FAB COMBINED.pdf` states 60 welds over two legible reports and has 86
    # shots, and the reports whose counts went unread explain the rest.
    database, pid = db
    sheet(database, pid, 18, page=7)
    sheet(database, pid, 42, page=8)
    shots(database, pid, [f"FFB-{n:03d}" for n in range(86)])
    assert run(database, pid) == []


def test_a_sheet_nothing_could_read_is_quantified(db):
    # `8-12-25 FPT 015-022 SEG E.pdf` is named for the day, so no shot is
    # attributed to it at all. The count says how much is missing.
    database, pid = db
    sheet(database, pid, 8, filename="8-12-25 FPT 015-022 SEG E.pdf")
    found = run(database, pid)
    assert len(found) == 1
    assert found[0]["detail"] and '"shortfall": 8' in found[0]["detail"]


# -- bundles ----------------------------------------------------------------

def test_several_reports_in_one_bundle_are_summed(db):
    database, pid = db
    sheet(database, pid, 37, page=3)
    sheet(database, pid, 9, page=4)
    shots(database, pid, ["FTI-036", "FTI-037", "FTI-038", "FTI-039", "FTI-039R"])
    found = run(database, pid)
    assert len(found) == 1
    assert "46 welds examined across 2 reports" in found[0]["message"]


def test_a_bundle_says_its_figure_is_a_lower_bound(db):
    database, pid = db
    sheet(database, pid, 37, page=3)
    sheet(database, pid, 9, page=4)
    shots(database, pid, ["FTI-036"])
    assert "lower bound" in run(database, pid)[0]["message"]


def test_a_single_report_makes_no_such_caveat(db):
    database, pid = db
    sheet(database, pid, 8)
    assert "lower bound" not in run(database, pid)[0]["message"]


# -- how many pages the report has ------------------------------------------
#
# The crew files a multi-page report as separate one-page PDFs, so a document
# holding "Page 8 of 8" alone proves nothing on its own. The ticket number is
# the only field that ties the pages back together.

def paged(db, pid, n, of, *, fp="fp1", page=1, ticket="18700172",
          filename="DML-18 10-9-25.pdf"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO reader_sheet(project_id, document_id, fingerprint,
                                        filename, segment, page_no, ticket,
                                        stated_page, stated_pages, evidence)
               VALUES(?, 1, ?, ?, 'SEG A', ?, ?, ?, ?, 'text')""",
            (pid, fp, filename, page, ticket, n, of),
        )


def pages(db, pid):
    return missing_report_pages(db, pid, "run")


def test_a_report_whose_other_pages_are_nowhere_is_reported(db):
    database, pid = db
    paged(database, pid, 8, 8)
    found = pages(database, pid)
    assert len(found) == 1 and found[0]["severity"] == "major"
    assert "1, 2, 3, 4, 5, 6, 7" in found[0]["message"]


def test_pages_filed_as_separate_documents_are_found(db):
    # Same ticket, three different PDFs. Nothing is missing.
    database, pid = db
    for i in range(1, 4):
        paged(database, pid, i, 3, fp=f"fp{i}", filename=f"page{i}.pdf")
    assert pages(database, pid) == []


def test_a_page_with_no_ticket_inherits_its_documents(db):
    # Page 1 of the Precision Group radiography report states its number but
    # not the ticket, and grouping strictly by the printed ticket reported it
    # as missing while it sat in the same PDF as pages 2 and 3.
    database, pid = db
    paged(database, pid, 1, 3, page=1, ticket="", filename="FFB-048.pdf")
    paged(database, pid, 2, 3, page=2, ticket="RT-1061-0794", filename="FFB-048.pdf")
    paged(database, pid, 3, 3, page=3, ticket="RT-1061-0794", filename="FFB-048.pdf")
    assert pages(database, pid) == []


def test_a_blank_ticket_is_reported_as_untraceable_not_as_missing(db):
    # `DBR 1P-8 7-16-25.pdf` says page 3 of 4 and leaves Ticket No empty, so
    # the other three pages cannot be chased either way.
    database, pid = db
    paged(database, pid, 3, 4, ticket="", filename="DBR 1P-8 7-16-25.pdf")
    found = pages(database, pid)
    assert len(found) == 1 and found[0]["severity"] == "minor"
    assert "cannot be traced" in found[0]["message"]


def test_two_reports_interleaved_in_one_bundle_are_kept_apart(db):
    # `FAB COMBINED.pdf` holds ticket 0964 on PDF pages 3, 4, 5 and 8 with
    # ticket 0965 in between. Requiring a report's pages to be contiguous in
    # the file would call both incomplete.
    database, pid = db
    for pdf_page, n in ((3, 1), (4, 2), (5, 3), (8, 4)):
        paged(database, pid, n, 4, page=pdf_page, ticket="RT-1061-0964",
              filename="FAB  COMBINED.pdf")
    for pdf_page, n in ((6, 1), (7, 2)):
        paged(database, pid, n, 2, page=pdf_page, ticket="RT-1061-0965",
              filename="FAB  COMBINED.pdf")
    assert pages(database, pid) == []


def test_a_single_page_report_is_not_a_finding(db):
    database, pid = db
    paged(database, pid, 1, 1)
    assert pages(database, pid) == []


def test_a_blank_pagination_says_nothing(db):
    # The labels are printed on 200 of 208 Bluewater sheets and filled in on
    # twelve. A blank is silence, not a claim of one page.
    database, pid = db
    sheet(database, pid, 4)          # weld count only, no pagination
    assert pages(database, pid) == []


# -- the ticket number itself -----------------------------------------------
#
# The ticket is the package's only join between the pages and the days of a
# report, so it is worth checking that it holds together.

def ticketed(db, pid, ticket, *, when="2025-09-09", fp=None, page=1,
             filename="20IN LP 09.09.25 GTI-025-029.pdf"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO reader_sheet(project_id, document_id, fingerprint,
                                        filename, segment, page_no, ticket,
                                        sheet_date, evidence)
               VALUES(?, 1, ?, ?, 'SEG A', ?, ?, ?, 'text')""",
            (pid, fp or f"fp{ticket}{page}", filename, page, ticket, when),
        )


def block(db, pid, n=12, start=18600280):
    """A settled house style for the 186 block."""
    for i in range(n):
        ticketed(db, pid, str(start + i), fp=f"blk{i}", filename=f"s{i}.pdf")


def test_a_ticket_shorter_than_its_block_is_reported(db):
    # `GXR-1P-7 10-27-25.pdf` really does print `Ticket No: 1860042` where its
    # neighbours print `18600289` — the crew dropped a digit, and a mistyped
    # ticket cannot be matched to the rest of its report.
    database, pid = db
    block(database, pid)
    ticketed(database, pid, "1860042", fp="odd", filename="GXR-1P-7 10-27-25.pdf")
    found = malformed_ticket(database, pid, "run")
    assert len(found) == 1
    assert "7 digits where the other 12 tickets in the 186 block are 8" \
        in found[0]["message"]


def test_a_well_formed_block_is_quiet(db):
    database, pid = db
    block(database, pid)
    assert malformed_ticket(database, pid, "run") == []


def test_blocks_are_judged_separately(db):
    # 172, 176, 180, 186, 187 and 189 are different issuing blocks; a block
    # with its own length is not evidence against another.
    database, pid = db
    block(database, pid)
    for i in range(4):
        ticketed(database, pid, str(1740000 + i), fp=f"other{i}",
                 filename=f"o{i}.pdf")
    assert [f["subject"] for f in malformed_ticket(database, pid, "run")] == []


def test_too_few_tickets_to_know_the_house_style(db):
    database, pid = db
    ticketed(database, pid, "18600289", fp="a")
    ticketed(database, pid, "1860042", fp="b")
    assert malformed_ticket(database, pid, "run") == []


def test_one_ticket_on_two_days_is_reported(db):
    # Both sheets print `Ticket No: 18600289`, five weeks apart, each "page 1
    # of 1". One of them carries the wrong number.
    database, pid = db
    ticketed(database, pid, "18600289", when="2025-09-09", fp="a")
    ticketed(database, pid, "18600289", when="2025-10-17", fp="b",
             filename="20IN LP 10.17.25 GPT-4.pdf")
    found = ticket_spans_days(database, pid, "run")
    assert len(found) == 1
    assert "2025-09-09 and 2025-10-17" in found[0]["message"]


def test_one_ticket_over_several_pages_of_one_day_is_fine(db):
    database, pid = db
    for p in (1, 2, 3):
        ticketed(database, pid, "18600289", page=p, fp="a")
    assert ticket_spans_days(database, pid, "run") == []


def test_a_bundles_days_do_not_spread_across_its_tickets(db):
    # `4IN FLEX FAB READERS.pdf` is one document holding fifteen days over
    # sixteen pages. Joining the date at document level gave every ticket in
    # the file all fifteen days and turned three findings into thirty-two.
    database, pid = db
    days = [f"2025-11-{d:02d}" for d in range(6, 21)]
    for i, day in enumerate(days, start=1):
        ticketed(database, pid, f"186005{i:02d}", when=day, page=i,
                 fp="bundle", filename="4IN FLEX FAB READERS.pdf")
    assert ticket_spans_days(database, pid, "run") == []


# -- tickets run in date order ----------------------------------------------
#
# An inference, not something a document states — but three of the six blocks
# in the corpus (24, 23 and 39 tickets) are in perfect order, and the
# exceptions are few and large.

def run_of(db, pid, start, first_day, n=12, step=1):
    """A block of consecutive tickets, one per day."""
    day = datetime.date.fromisoformat(first_day)
    for i in range(n):
        ticketed(db, pid, str(start + i * step),
                 when=(day + datetime.timedelta(days=i)).isoformat(),
                 fp=f"seq{start}{i}", filename=f"sheet{start + i}.pdf")


def order(db, pid):
    return ticket_out_of_order(db, pid, "run")


def test_a_block_in_order_is_quiet(db):
    database, pid = db
    run_of(database, pid, 18600280, "2025-09-01")
    assert order(database, pid) == []


def test_a_ticket_dated_long_before_its_neighbours_is_reported(db):
    # `GFB-113 9-27-25.pdf` carries ticket 18600502, whose neighbours were all
    # worked in late November.
    database, pid = db
    run_of(database, pid, 18600490, "2025-11-15")
    ticketed(database, pid, "18600502", when="2025-09-27", fp="odd",
             filename="GFB-113 9-27-25.pdf")
    run_of(database, pid, 18600512, "2025-12-01")
    found = order(database, pid)
    assert [f["subject"] for f in found] == ["GFB-113 9-27-25.pdf"]
    assert "earlier than all five tickets below it" in found[0]["message"]


def test_a_ticket_dated_long_after_its_neighbours_is_reported(db):
    # `20IN LP 08.26.25 CXR-034-044.pdf` is dated 08/26/26 in a job that ran
    # through 2025 — and its own filename says 25.
    database, pid = db
    ticketed(database, pid, "18001773", when="2026-08-26", fp="odd",
             filename="20IN LP 08.26.25 CXR-034-044.pdf")
    run_of(database, pid, 18001774, "2025-08-25")
    found = order(database, pid)
    assert [f["subject"] for f in found] == ["20IN LP 08.26.25 CXR-034-044.pdf"]
    assert "later than all five tickets above it" in found[0]["message"]


def test_a_one_day_inversion_is_not_a_finding(db):
    # Three sheets written up on the 10th and one on the 9th, numbered
    # between them. A global sort called this out of order; the crew simply
    # wrote one up the next morning.
    database, pid = db
    run_of(database, pid, 18001860, "2025-10-01")
    ticketed(database, pid, "18001867", when="2025-10-09", fp="a",
             filename="CFB-10,14-17 10-9-25.pdf")
    for i, t in enumerate(("18001868", "18001869", "18001870")):
        ticketed(database, pid, t, when="2025-10-10", fp=f"b{i}")
    run_of(database, pid, 18001880, "2025-10-11")
    assert [f["subject"] for f in order(database, pid)] == []


def test_one_bad_sheet_does_not_implicate_its_neighbours(db):
    # Unanimity against a window is what keeps this to one finding: a sheet
    # adjacent to a wildly misdated one is still in order with the rest.
    database, pid = db
    run_of(database, pid, 18600490, "2025-11-15")
    ticketed(database, pid, "18600502", when="2025-09-27", fp="odd")
    run_of(database, pid, 18600512, "2025-12-01")
    assert len(order(database, pid)) == 1


def test_a_short_block_is_not_judged(db):
    # Fewer than eleven tickets and there is no neighbourhood to be out of.
    database, pid = db
    run_of(database, pid, 18600280, "2025-09-01", n=4)
    ticketed(database, pid, "18600290", when="2024-01-01", fp="odd")
    assert order(database, pid) == []


def test_what_the_other_ticket_rules_own_is_left_to_them(db):
    # A mistyped ticket and a ticket covering two days are both out of order
    # by construction; NDE-15 and NDE-16 report them with the right reason.
    database, pid = db
    run_of(database, pid, 18600280, "2025-09-01")
    ticketed(database, pid, "1860042", when="2024-01-01", fp="short")
    ticketed(database, pid, "18600291", when="2024-01-02", fp="d1")
    ticketed(database, pid, "18600291", when="2025-06-02", fp="d2")
    assert order(database, pid) == []


def test_each_filing_copy_is_judged_on_its_own_content(db):
    # Two different files are both named `FAB COMBINED.pdf`, with different
    # fingerprints. Pooling them would double every stated count.
    database, pid = db
    sheet(database, pid, 60, fp="fpA", filename="FAB  COMBINED.pdf")
    sheet(database, pid, 60, fp="fpB", filename="FAB  COMBINED.pdf")
    shots(database, pid, [f"FFB-{n:03d}" for n in range(70)], fp="fpA")
    shots(database, pid, [f"FFB-{n:03d}" for n in range(70)], fp="fpB")
    assert run(database, pid) == []
