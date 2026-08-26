# WeldAudit

Audits a pipeline turnover package without opening every document.

Point it at a job folder. It reads the weld maps, the NDE package, the material
certificates, the pressure tests, the coating reports, the flange logs, the
welder qualifications, the backfill releases and the as-built, and reports
where they disagree with each other.

It runs entirely on the machine it is installed on. Nothing is uploaded, and
nothing in the job folder is modified.

## Why

A turnover package is a few thousand PDFs and spreadsheets that are supposed to
tell one consistent story: this weld, made by this welder to this procedure,
inspected on this shot, in pipe from this heat, from a mill on the approved
list, in a ditch released for backfill, at this station on the as-built.

Checking that by hand means holding several registers in your head at once, and
the failures are quiet ones — a heat on the drawing that no certificate covers,
a shot cited on a weld map that no reader sheet reports, a mill nobody noticed
had come off the approved list. This finds those and says where to look.

## Running it

Download `WeldAudit.exe` and double-click it. There is no installer and it
needs no administrator rights — which matters on a managed laptop, where an
installer is exactly what you cannot run.

It opens a window, asks which folder to audit, and puts its findings in a table
you can filter, sort and export to Excel or CSV.

From source instead:

```bash
pip install -r requirements.txt
python -m weldaudit audit "C:\Jobs\Line 12" --excel "Line 12 exceptions.xlsx"
```

Everything it stores goes in `~/.weldaudit/`.

## What it checks

Around 140 rules across eleven families. Each one reports what it found, which
document it found it in, and what would close it.

| family | asks |
|---|---|
| **NDE** | does every weld cite a shot, and does every shot exist? Both directions — a shot with no weld is as wrong as a weld with no shot |
| **WeldTrace** | where the registers arrive as a WeldTrace export, do its three registers agree with each other and with the stamps on the isometrics? |
| **Materials** | is every heat on the drawing certified, and is every certified mill on the approved list? |
| **Welders** | was each welder qualified for what they welded, on the date they welded it, within the range their test covers? |
| **Procedures** | is every procedure cited on a weld map actually filed? |
| **Pressure tests** | was each segment tested, for long enough, at the right pressure, with calibrated instruments? |
| **Coating** | holiday tests, thickness readings, and the environment they were taken in |
| **Flanges** | bolt-up records, torque wrench calibration, inspector sign-off |
| **Backfill** | was the ditch released before it was covered? |
| **As-built** | do the stations agree with the pipe laid between them? |
| **Completeness** | which sections of the book are missing outright |

The tone throughout is that a finding must be actionable. "7 heats could not be
checked" is not a finding; "these seven certificates, at these paths, have no
readable manufacturer" is.

## WeldTrace downloads

Where a job keeps its registers in WeldTrace rather than on paper, point the
audit at the unpacked download and it reads the exports directly. Nothing needs
renaming or filing into the book first — the three machine-readable exports are
recognised by the names WeldTrace gives them.

| export | carries |
|---|---|
| `*TestPackExport.csv` | the weld register — one row per weld, with both heats, the procedure, a welder per pass, and eight examination blocks |
| `*projectMaterialsExport.csv` | the heat register, each heat pointing at its MTR |
| `AnnotationAttachments_*.pdf` | the as-built — every weld tag, welder and date stamped on the isometrics |

The signed drawing set and the QAQC-FRM-4347 test plan are stored rather than
parsed. Rewriting a signed PDF would break the seal that makes it worth
storing.

A WeldTrace weld is not a twelfth kind of audit — it is a weld register that
arrives *typed instead of scanned*, so it fills the same tables a daily report
does and every existing rule and tab reads it unchanged. It also means none of
this needs the OCR pass below, which matters more than it sounds: see
[Moving the readings between machines](#moving-the-readings-between-machines)
for what an unread package does to a report.

Twenty-one rules on top, `WT-01` to `WT-21`, all of them comparisons between
two of the three registers:

| asks | reports |
|---|---|
| is the download complete? | an export that never arrived, said once, rather than every weld failing for want of it |
| is each weld attributable? | no procedure, an unmanned root/fill/cap pass, no weld date or one no parser accepts |
| do the joints match the heat register? | a heat missing, a heat in no register, a product form the two disagree about, a heat no longer active |
| is the material evidence there? | a heat with no MTR attached, and heats whose supplier, spec, grade or P-number are blank so approval **cannot be evaluated at all** |
| were the examinations done? | requested and never reported, failed with no retest, a verdict that is neither a pass nor a fail, a report number that disagrees with its test pack, a weld nothing was asked of |
| does the register match the drawings? | a weld stamped on no isometric, one stamped on a different isometric than the register names, and a stamp that is in no test pack |

The blank-fields rule is the one that earns its keep. Those four fields are
optional in WeldTrace, and on the first download seen all four were blank on
every heat — so the approved-manufacturer check had nothing to evaluate and
would have reported nothing at all. That is an export setting rather than a
missing document, and the audit says so instead of falling silent.

Where the register and the drawings disagree, both sides are reported and
neither is resolved. `W-81` in the register against `BFW-81` on the drawing is
almost certainly one joint mistyped — but *almost* is not a thing to write into
a turnover package, and a tool that paired those automatically would also pair
the two welds that genuinely are different.

## Reading scanned documents

Over half the PDFs in a typical package are images with no text layer, so most
of the above runs without touching them — filenames, spreadsheets and CSV
exports carry more than you would expect.

For the rest there are three readers, cheapest first:

1. **The text layer**, where there is one. Free and exact.
2. **OCR on this machine** — bundled, offline, free, a few seconds a page. It
   records a letterhead only where it can match one against the approved list,
   and refuses the rest, so a poor scan cannot invent a manufacturer.
3. **A hosted vision model**, off unless you set `ANTHROPIC_API_KEY`. No key
   ships with the program.

Every reading is cached by the **hash of the page**, so a document read once is
never read again — including the eleven copies of one reader sheet that a
package will file across six different books.

### Moving the readings between machines

The cache is the expensive part and it does not travel with the program: it
lives in each user's profile. Give someone the exe alone and their scanned
certificates are never read, so the approved-list checks cannot run — and their
report comes back *shorter*, which reads like a cleaner package rather than a
blinder audit.

```bash
weldaudit cache export readings.wacache      # on the machine that has them
weldaudit cache import readings.wacache      # on the one that does not
```

The program also offers to load a `.wacache` it finds beside itself, so on a
fresh machine this is one click rather than a command nobody runs.

## The approved manufacturer list

Put the issued AML PDF anywhere above your job folders and the audit finds it,
walking up from the job. A transcribed spreadsheet works too.

Prefer the PDF. Only the PDF states the date the list stops being valid, and
only then can the program tell you that the list you are auditing against has
expired — a spreadsheet carries no such date, and an audit run against a
three-year-old copy looks exactly like one run this morning. Where both are
present the newest dated revision wins.

Without any list, everything else still runs and the manufacturer checks are
reported as *skipped* rather than passing silently.

## Layout

```
weldaudit/
  index.py        walks the folder, classifies documents, fingerprints them
  extract/        one module per document kind, filename and content
  rules/          one module per family; each rule is a function
  weldtrace.py    parses a WeldTrace export, and the quirks in one
  aml.py          approved-list matching
  amlpdf.py       reads the issued AML out of its PDF
  report.py       Excel and CSV
  web/            the interface, one HTML file
app.py            the desktop window
eval/             scoring harness for comparing readers
tests/            ~1,400 tests
```

Rules register themselves, so adding one is a function and a decorator; nothing
central needs editing.

## Tests

```bash
python -m pytest -q
```

They run without any customer data. A few integration tests look for an
approved list on the machine and take their names out of whichever one they
find, so they hold for any site's list rather than a particular one; without a
list they skip.

## Before you commit

```bash
git config core.hooksPath tools/hooks
```

Once per clone. It refuses a commit carrying a secret, an unreviewed binary,
or anything matching `private/forbidden.txt` — a gitignored list of names that
must not be published. The list is not in the repository, because a list of a
customer's names is itself the thing being protected.

The binary rule is the one that earns its keep. A `.docx` holding a database
password once reached a public commit here: a review that reads the text diff
is structurally blind to a zip, and no amount of care fixes that.

## A note on the fixtures

Every manufacturer, heat number, job and contractor in this repository is
invented. The *shapes* are not: each filename convention, garbled letterhead
and awkward spreadsheet layout was taken from a real one and then renamed,
because those shapes are what the code exists to survive.

## Licence

MIT — see [LICENSE](LICENSE). Use it, change it, ship it; it comes with no
warranty, which for a tool that reads safety records is worth reading rather
than skipping.
