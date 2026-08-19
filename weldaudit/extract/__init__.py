"""Document extractors.

Each module turns one family of source document into rows in the audit
database.  Extractors are ordered cheapest-first: filename parsing, then
spreadsheet parsing, then text-layer PDF, and only then vision OCR.
"""

from . import dwr, materials, ndelog, readersheets, weldlog_csv, welders  # noqa: F401

__all__ = ["dwr", "materials", "ndelog", "readersheets", "weldlog_csv", "welders"]
