"""Telling an NDE rig log apart from a technician's certificate.

They live in the same folder and the folder is named after both. One package
files them under `11-NDE\\Tech Log_Tech Certs\\`, with the log in `Tech Log\\`
and the certificates in `Certs\\`.

The log was named `JOB 7 NDE Tech Log.xlsx` - *tech* log, not *rig* log - so
it matched nothing by filename, the containing folder was consulted instead,
and `tech certs` in that folder name filed the rig log as a certificate. The
reader only ever looks at kind `nde_rig_log`, so it read nothing; NDT-06 then
reported that the job had no rig log, on a job carrying one in every segment
book. The sheet inside is called "NDE Rig Log" and its header reads PROJECT
NDE TECH RIG LOG.

The cost of getting this wrong runs both ways, so both directions are pinned
below: a log read as a certificate loses every technician check, and a
certificate read as a log would be handed to openpyxl as a spreadsheet.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from weldaudit.taxonomy import kind_for  # noqa: E402

JOB = r"C:\Jobs\JOB 7\16IN XX ALL FILES\11-NDE"


@pytest.mark.parametrize("path", [
    rf"{JOB}\Tech Log_Tech Certs\Tech Log\JOB 7 NDE Tech Log.xlsx",
    rf"{JOB}\Tech Log_Tech Certs\JOB 7 NDE Tech Log.xlsx",
    rf"{JOB}\NDE Rig Log.xlsx",
    rf"{JOB}\NDE Log.xlsx",
    rf"{JOB}\Rig Log.xlsx",
])
def test_a_rig_log_is_a_rig_log_whatever_it_is_called(path):
    assert kind_for(path) == "nde_rig_log"


@pytest.mark.parametrize("path", [
    rf"{JOB}\Tech Log_Tech Certs\Certs\Marco Reyes Vision Exam 5.1.2025.pdf",
    rf"{JOB}\Tech Log_Tech Certs\Certs\Reyes Marco 2024 Northfield Inspection Cert.pdf",
    rf"{JOB}\Tech Log_Tech Certs\Certs\Marco Silva NFI Certs..Exp.10.22.26.pdf",
    rf"{JOB}\NDE Certs\Elena Marsh.pdf",
])
def test_a_technician_certificate_is_not_a_rig_log(path):
    """The expensive half of the fix: widening the log pattern must not drag
    the certificates in with it. They sit in a folder whose name says both."""
    assert kind_for(path) == "nde_tech_cert"


@pytest.mark.parametrize("path,kind", [
    (rf"{JOB}\Reader Sheets\AFB-001.pdf", "nde_reader_sheet"),
    (rf"{JOB}\Procedures\Company Procedures\RT Procedure.pdf", "nde_procedure"),
])
def test_the_rest_of_the_nde_section_is_unmoved(path, kind):
    assert kind_for(path) == kind


def test_the_filename_still_beats_the_folder():
    """A rig log filed in the certificates folder is still a rig log."""
    assert kind_for(rf"{JOB}\Tech Log_Tech Certs\Certs\NDE Rig Log.xlsx") == "nde_rig_log"
