"""Command line entry point:  python -m weldaudit <command>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import Database
from .extract.vision_pass import TARGETS
from .pipeline import default_db_path, run, summary
from .report import write_excel
from .vision import (
    DEFAULT_MAX_EDGE, DEFAULT_MODEL, LOCAL_MAX_EDGE, MODEL_PRICES, TILE_MODES,
    TILED_KINDS, is_local,
)


def _fmt(n: int) -> str:
    return f"{n:,}"


def cmd_audit(args: argparse.Namespace) -> int:
    db = Database(args.db or default_db_path())
    name = args.name or Path(args.root).name

    def progress(stage: str, msg: str) -> None:
        print(f"  [{stage:<9}] {msg}", flush=True)

    # A folder already audited keeps the name it was stored under, so the name
    # asked for here may not be the one the later commands want. Saying which
    # was used costs a line; not saying it costs a puzzling "No project named".
    already = db.project_at(args.root)
    if already is not None and already["name"] != name:
        print(f"Re-auditing '{already['name']}', already stored for this folder"
              f" (asked for '{name}').")
        name = already["name"]

    print(f"Auditing '{name}'  ({args.root})")
    result = run(db, name, args.root, only_rules=args.rules, progress=progress)

    print(f"\nExtracted: {_fmt(result.counts.get('dwr_welds', 0))} welds from "
          f"{result.counts.get('dwr_files', 0)} daily weld reports, "
          f"{_fmt(result.counts.get('csv_welds', 0))} welds from "
          f"{result.counts.get('csv_files', 0)} log exports, "
          f"{_fmt(result.counts.get('nde_shots', 0))} NDE shots on file.")

    sev = result.by_severity
    print("\nFindings: " + (", ".join(f"{sev[s]} {s}" for s in
          ("critical", "major", "minor", "info") if s in sev) or "none"))

    by_rule: dict[str, int] = {}
    for f in result.findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    for code in sorted(by_rule):
        print(f"  {code}  {by_rule[code]:>5}")

    if args.top:
        print(f"\nTop {args.top} findings:")
        for f in result.findings[: args.top]:
            print(f"  [{f['severity']:<8}] {f['segment'][:26]:<28} {f['message'][:96]}")

    if args.excel:
        out = write_excel(db, result.project_id, args.excel)
        print(f"\nWrote {out}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    """List audited jobs, or print one job's folder.

    Exists so the launcher scripts do not have to embed Python one-liners in
    batch files. A one-liner holding brackets, quotes and semicolons is not
    something ``cmd``'s ``for /f`` can parse, and the failure looks like a bug
    in the audit rather than in the quoting.
    """
    db = Database(args.db or default_db_path())
    if args.root:
        row = db.one("SELECT root FROM project WHERE name=?", (args.root,))
        if not row:
            print(f"No project named '{args.root}'.", file=sys.stderr)
            return 1
        print(row["root"])
        return 0
    for row in db.q("SELECT name FROM project ORDER BY name"):
        print(row["name"])
    return 0


def cmd_release(args) -> int:
    """Package a build into a shared folder for every other copy to pick up.

    The publishing half of the updater, and the only part a person runs. The
    archive and the version file are written together on purpose: a
    version.json that disagrees with its archive is a broken update on
    somebody else's machine, discovered by them.
    """
    from .update import INSTALLER, current_version, publish

    build = Path(args.build)
    if not build.exists():
        print(f"No build at {build}. Run Build.bat first.", file=sys.stderr)
        return 1

    version = args.version or current_version()
    archive = publish(build, args.to, version, notes=args.notes or "",
                      installer=args.installer)
    size = archive.stat().st_size / (1024 * 1024)
    print(f"WeldAudit {version} published to {args.to}")
    print(f"  {archive.name}  ({size:,.0f} MB)")

    # Said out loud either way. A folder with a current archive and no
    # installer is fine; a person who assumed it had one is not.
    setup = Path(args.to) / INSTALLER
    if setup.is_file():
        print(f"  {setup.name}  ({setup.stat().st_size / (1024 * 1024):,.0f} MB)"
              f"  - for a first install")
    else:
        print(f"  no {INSTALLER} beside the build, so none was published.")
        print(f"  Anyone who has never installed WeldAudit will need one.")
    print()
    print("Share that folder with whoever runs WeldAudit. Their copy reads it")
    print("on startup and offers the update; nothing is downloaded from the")
    print("internet and no password is needed.")
    return 0


def cmd_cache(args) -> int:
    """Move page readings between machines.

    The cache is keyed by the hash of the page, so a reading holds wherever
    that document sits. It is also the expensive part: most of it was read a
    page at a time by a paid model, and none of it travels with the exe.
    """
    db = Database(args.db or default_db_path())
    if args.action == "export":
        n = db.export_cache(args.file)
        size = Path(args.file).stat().st_size / 1024
        print(f"{n:,} page readings written to {args.file} ({size:,.0f} KB)")
        print("Load it on the other machine with:  weldaudit cache import <file>")
        return 0

    try:
        got = db.import_cache(args.file)
    except (FileNotFoundError, ValueError) as bad:
        print(str(bad), file=sys.stderr)
        return 1
    print(f"{got['in_the_file']:,} readings in the file: "
          f"{got['added']:,} added, {got['already_here']:,} already here")
    if got["added"]:
        print("Re-run 'audit' to fold them in.")
    return 0


def cmd_aml(args) -> int:
    """Read an the operator Piping AML PDF: check it, compare it, export it."""
    from . import amlpdf
    from .extract.materials import find_aml_workbook

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"No such file: {pdf}", file=sys.stderr)
        return 1
    if not amlpdf.looks_like_an_aml(pdf):
        print(f"{pdf.name} does not look like a Piping AML.", file=sys.stderr)
        return 1

    said, on = amlpdf.revision(pdf)
    rows = amlpdf.parse(pdf)
    complaints, per = amlpdf.check(rows)

    print(f"{pdf.name}")
    print(f"  revision   {said or '(not stated)'}")
    late = amlpdf.expired(on)
    if late:
        print(f"  EXPIRED    {late} days ago")
    elif on:
        print(f"  valid thru {on.isoformat()}")
    print(f"  entries    {len(rows):,}")
    for name, n in sorted(per.items()):
        print(f"     {name:28} {n:>5}")
    for name in amlpdf.NO_LIST:
        print(f"     {name:28} {'-':>5}  (no manufacturer list in this revision)")

    if complaints:
        # Printed, not raised: knowing which section failed is the whole point.
        print()
        print("REFUSED - this parse is not safe to use:", file=sys.stderr)
        for c in complaints:
            print(f"  - {c}", file=sys.stderr)
        return 1

    # What changes against the list an audit would use today.
    against = Path(args.against) if args.against else find_aml_workbook(Path.cwd())
    if against and against.exists() and against != pdf:
        from .aml import Aml

        try:
            if against.suffix.lower() == ".pdf":
                before = [(e.manufacturer, e.location) for e in amlpdf.entries(against)]
            else:
                before = [(e.manufacturer, e.location)
                          for e in Aml.from_workbook(against).entries]
        except Exception as bad:          # noqa: BLE001 - unreadable comparison
            print(f"  (could not read {against.name} to compare: {bad})")
            before = None
        if before is not None:
            added, removed = amlpdf.compare(rows, before)
            print()
            print(f"  against {against.name}: {len(added)} added, {len(removed)} removed")
            for man, loc, _cat in sorted(added, key=lambda x: x[0].lower())[:args.show]:
                print(f"     + {man[:44]:46} {loc[:28]}")
            for man, loc in sorted(removed, key=lambda x: x[0].lower())[:args.show]:
                print(f"     - {man[:44]:46} {loc[:28]}")
            if max(len(added), len(removed)) > args.show:
                print(f"     ... --show {max(len(added), len(removed))} for the rest")

    if args.to:
        out = amlpdf.write_workbook(pdf, args.to, previous=locals().get("before"))
        print()
        print(f"  written to {out}")
    return 0


def cmd_rename(args) -> int:
    """Give a stored audit a different name."""
    db = Database(args.db or default_db_path())
    row = db.one("SELECT id, name FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'.", file=sys.stderr)
        return 1
    try:
        stored = db.rename_project(row["id"], args.to)
    except ValueError as bad:
        print(str(bad), file=sys.stderr)
        return 1
    print(f"'{row['name']}' is now '{stored}'")
    return 0


def cmd_forget(args) -> int:
    """Remove a stored audit. Nothing on disk is touched."""
    db = Database(args.db or default_db_path())
    row = db.one("SELECT id, name, root FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'.", file=sys.stderr)
        return 1

    stored = db.stored_for(row["id"])
    typed = stored.get("correction", 0) + stored.get("vendor_reading", 0)
    print(f"{row['name']}  ({row['root']})")
    print(f"  {stored.get('finding', 0)} findings, "
          f"{stored.get('document', 0)} indexed documents")
    if typed:
        print(f"  {typed} values typed by hand, which auditing again will not "
              f"bring back")
    print(f"  {db.cached_pages()} cached page readings are kept either way")

    if not args.yes:
        # Not a prompt: a windowed exe attached to a console cannot be relied
        # on to read stdin, and a delete that hangs waiting for an answer
        # nobody can give is worse than one that refuses.
        print("Nothing removed. Add --yes to go ahead.", file=sys.stderr)
        return 1

    removed = db.delete_project(row["id"])
    print(f"Removed {row['name']}: "
          + ", ".join(f"{n} {t}" for t, n in sorted(removed.items())))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    db = Database(args.db or default_db_path())
    row = db.one("SELECT id FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'. Run 'audit' first.", file=sys.stderr)
        return 1
    data = summary(db, int(row["id"]))
    print(f"{'segment':<40}{'welds':>7}{'w/NDE':>7}{'sheets':>8}{'%ref':>6}")
    for c in data["coverage"]:
        print(f"{c['segment'][:38]:<40}{c['welds']:>7}{c['welds_with_nde']:>7}"
              f"{c['sheets_on_file']:>8}{c['pct_referenced']:>5}%")
        if c["multiple_registers"]:
            # The headline is an estimate when two registers describe the same
            # welds; show what it was derived from rather than just the number.
            for reg in c["registers"]:
                print(f"  {reg['register'][:36]:<38}{reg['welds']:>7}"
                      f"{reg['welds_with_nde']:>7}")
    if any(c["multiple_registers"] for c in data["coverage"]):
        print("\nIndented rows are the individual registers. The segment figure is a "
              "deduplicated\nestimate, not their sum — see REG-03.")
    return 0


def cmd_vision(args: argparse.Namespace) -> int:
    """Read scanned pages with a vision model. Always previews cost first."""
    from .extract.vision_pass import estimate_pass, run
    from .vision import VisionReader, VisionUnavailable, credentials_available

    db = Database(args.db or default_db_path())
    row = db.one("SELECT id FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'. Run 'audit' first.", file=sys.stderr)
        return 1
    project_id = int(row["id"])
    if args.max_edge is None:
        args.max_edge = LOCAL_MAX_EDGE if is_local(args.model) else DEFAULT_MAX_EDGE

    est, targets = estimate_pass(
        db, project_id, args.kind,
        model=args.model, max_edge=args.max_edge, limit=args.limit,
        tiles=args.tiles,
    )
    print(f"Vision pass '{args.kind}' on '{args.name}'")
    print(f"  {est.describe()}")
    if args.kind == "mtr":
        print("  (upper bound — reading stops once a certificate gives its heat "
              "and issuer, so most cost less)")
    if targets:
        print("\nTop targets:")
        for t in targets[:8]:
            print(f"  {t.filename[:58]:<60} {t.pages}p  ({t.reason})")
        if len(targets) > 8:
            print(f"  ... and {len(targets) - 8:,} more")

    if not est.pages:
        print(f"\nNo '{args.kind}' documents in this project need reading.")
        return 0
    if not est.pages_to_read:
        print("\nNothing to read — every target is already in the cache.")
        return 0
    if args.dry_run:
        print("\nDry run: nothing was sent. Re-run without --dry-run to proceed.")
        return 0

    # Credentials are a hosted-model concern. A local model needs a daemon
    # instead, and demanding an API key for it would keep the pass shut on
    # exactly the installs it was added to serve.
    if is_local(args.model):
        from .vision import local_available

        ready, served = local_available()
        if not ready:
            print(f"\nNo local model available: {served}\n"
                  "  Install Ollama, then: ollama pull qwen2.5vl:7b",
                  file=sys.stderr)
            return 2
    elif not credentials_available():
        print(
            "\nNo Anthropic credentials found, so nothing was sent.\n"
            "  Set ANTHROPIC_API_KEY in your environment, or run 'ant auth login'.\n"
            "  Or read on this machine for nothing: --model local:qwen2.5vl:7b\n"
            "Everything else in WeldAudit works without this.",
            file=sys.stderr,
        )
        return 2

    if not args.yes:
        price = ("on this machine" if is_local(args.model)
                 else f"~${est.cost_usd:,.2f}")
        answer = input(f"\nRead {est.pages_to_read:,} pages ({price})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled — nothing was sent.")
            return 0

    reader = VisionReader(db, model=args.model, max_edge=args.max_edge,
                          tiles=args.tiles)

    def progress(i: int, total: int, name: str) -> None:
        print(f"  [{i:>4}/{total}] {name[:64]}", flush=True)

    try:
        result = run(db, project_id, args.kind, reader, targets, progress)
    except VisionUnavailable as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    print(f"\nRead {result.pages_read:,} pages ({result.pages_cached:,} from cache) "
          f"across {result.documents:,} documents; updated {result.updated:,} records.")
    if result.conflicts:
        print(f"{result.conflicts:,} value(s) read two different ways on separate "
              f"close-ups and left blank; they come back as VIS-01 findings.")
    if result.failures:
        print(f"{len(result.failures)} page(s) could not be read:")
        for f in result.failures[:10]:
            print(f"  {f}")
    print("\nRe-run 'audit' to reconcile with the new data.")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    """Read scanned certificates on this machine, for nothing."""
    from .extract import mtrocr
    from .rules.materials import _aml_from_db

    db = Database(args.db or default_db_path())
    row = db.one("SELECT id FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'. Run 'audit' first.", file=sys.stderr)
        return 1
    project_id = int(row["id"])

    ready, why = mtrocr.available()
    if not ready:
        print(f"{why}.", file=sys.stderr)
        return 2

    aml = _aml_from_db(db, project_id)
    if aml is None:
        # Without it there is nothing to recognise a name against, and a
        # letterhead nothing can confirm is not worth recording.
        print("No approved materials list loaded for this project, so a "
              "scanned name could not be confirmed against anything.",
              file=sys.stderr)
        return 2

    targets = mtrocr.scanned_targets(db, project_id, args.limit)
    print(f"OCR pass on '{args.name}'")
    print(f"  {len(targets):,} scanned certificates with no manufacturer, "
          f"about {len(targets) * 9 / 60:.0f} minutes on this machine, free")
    if not targets:
        print("\nNothing to read.")
        return 0
    if args.dry_run:
        print("\nDry run: nothing was read.")
        return 0

    def progress(i: int, total: int, name: str) -> None:
        print(f"  [{i:>4}/{total}] {name[:64]}", flush=True)

    done = mtrocr.read_scans(db, project_id, aml, args.limit, progress)
    print(f"\nRead {done['read']:,} scans ({done['cached']:,} already cached); "
          f"named a manufacturer on {done['named']:,}.")
    print("\nRe-run 'audit' to fold them in.")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    """Record what a person read off a page, overriding every reader."""
    from .extract.corrections import CORRECTABLE, listing, record

    db = Database(args.db or default_db_path())
    row = db.one("SELECT id FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'. Run 'audit' first.", file=sys.stderr)
        return 1
    project_id = int(row["id"])

    if args.list or not args.document:
        entries = listing(db, project_id)
        if not entries:
            print("Nothing has been corrected by hand on this job.")
            return 0
        print(f"{'field':16}{'value':32}{'document'}")
        for e in entries:
            print(f"{e['field']:16}{str(e['value'])[:30]:32}"
                  f"{(e['filename'] or e['fingerprint'])[:44]}")
        return 0

    docs = db.q(
        """SELECT DISTINCT fingerprint, filename FROM document
           WHERE project_id=? AND filename LIKE ? AND IFNULL(fingerprint,'')<>''""",
        (project_id, f"%{args.document}%"))
    if not docs:
        print(f"No document matching '{args.document}'.", file=sys.stderr)
        return 1
    if len({d["fingerprint"] for d in docs}) > 1 and not args.all:
        print(f"'{args.document}' matches {len(docs)} different documents:",
              file=sys.stderr)
        for d in docs[:10]:
            print(f"   {d['filename']}", file=sys.stderr)
        print("Be more specific, or pass --all to correct every one.",
              file=sys.stderr)
        return 1

    if args.field not in CORRECTABLE:
        print(f"Cannot correct '{args.field}'. Try: {', '.join(CORRECTABLE)}",
              file=sys.stderr)
        return 1

    seen = set()
    for d in docs:
        if d["fingerprint"] in seen:
            continue
        seen.add(d["fingerprint"])
        record(db, project_id, d["fingerprint"], args.field, args.value, args.note)
        print(f"  {args.field} = {args.value!r}  on {d['filename'][:56]}")
    print(f"\n{len(seen)} correction(s) recorded. Re-run 'audit' to apply them.")
    return 0


def cmd_vendor(args: argparse.Namespace) -> int:
    """Say which mill a letterhead this job cannot spell belongs to."""
    from .extract.readings import apply_readings, forget, listing, propose, record

    db = Database(args.db or default_db_path())
    row = db.one("SELECT id FROM project WHERE name=?", (args.name,))
    if not row:
        print(f"No project named '{args.name}'. Run 'audit' first.", file=sys.stderr)
        return 1
    pid = int(row["id"])

    if args.list or not args.reads:
        entries = listing(db, pid)
        if not entries:
            print("No letterheads have been identified by hand on this job.")
            return 0
        print(f"{'read as':32}{'is really':32}note")
        for e in entries:
            print(f"{e['as_read'][:30]:32}{e['manufacturer'][:30]:32}{(e['note'] or '')[:34]}")
        return 0

    if args.forget:
        forget(db, pid, args.reads)
        print(f"Forgot '{args.reads}'. Re-run 'audit' to undo its effect.")
        return 0

    if not args.is_really:
        print("Say what it is with --is \"Tex Tubo\".", file=sys.stderr)
        return 1

    record(db, pid, args.reads, args.is_really, args.note)
    print(f"  '{args.reads}' is {args.is_really}")

    # Suggestions, never applied on their own. The filename family that holds
    # these Tex-Tubo certificates also holds a Borusan Mannesmann one, and a
    # sweep by resemblance would have relabelled it.
    similar = propose(db, pid, args.reads)
    if similar:
        print(f"\nOther unconfirmed names on this job that look like it:")
        for name, n, score in similar:
            print(f"   {score:>3}%  {name[:40]:42} on {n} certificate(s)")
        if args.accept_similar:
            for name, _n, _s in similar:
                record(db, pid, name, args.is_really,
                       args.note or f"same letterhead as '{args.reads}'")
            print(f"\nAccepted all {len(similar)}.")
        else:
            print(f"\nNone of those were recorded. Add --accept-similar to take "
                  f"them all,\nor repeat --reads for the ones you have checked.")

    applied = apply_readings(db, pid)
    print(f"\n{applied} certificate(s) updated. Re-run 'audit' to reconcile.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import create_app

    app = create_app(args.db or default_db_path())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("weldaudit", description=__doc__)
    p.add_argument("--db", help="path to the audit database")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="index, extract and reconcile a project folder")
    a.add_argument("root", help="project folder to audit")
    a.add_argument("--name", help="project name (defaults to folder name)")
    a.add_argument("--rules", nargs="*", help="limit to these rule codes")
    a.add_argument("--excel", help="write an Excel exception report here")
    a.add_argument("--top", type=int, default=15, help="print the top N findings")
    a.set_defaults(func=cmd_audit)

    s = sub.add_parser("summary", help="coverage summary for an audited project")
    s.add_argument("name")
    s.set_defaults(func=cmd_summary)

    r = sub.add_parser("release",
                       help="publish a build to a shared folder for auto-update")
    r.add_argument("--build", default="dist/WeldAudit",
                   help="the folder build to package (default: dist/WeldAudit)")
    r.add_argument("--to", required=True, metavar="FOLDER",
                   help=r'the shared release folder, e.g. '
                        r'"%%USERPROFILE%%\OneDrive\WeldAudit Release"')
    r.add_argument("--version", help="version number (default: the program's own)")
    r.add_argument("--installer", metavar="EXE",
                   help="the setup to publish alongside the archive "
                        "(default: WeldAudit-Setup.exe beside the build)")
    r.add_argument("--notes", help="one line on what changed, shown to the user")
    r.set_defaults(func=cmd_release)

    q = sub.add_parser("cache", help="move page readings between machines")
    q.add_argument("action", choices=("export", "import"))
    q.add_argument("file", help="the cache file to write or read")
    q.set_defaults(func=cmd_cache)

    z = sub.add_parser("aml", help="read an the operator Piping AML PDF")
    z.add_argument("pdf", help="the Piping AML PDF")
    z.add_argument("--to", metavar="FILE.xlsx",
                   help="also write it out as a spreadsheet")
    z.add_argument("--against", metavar="FILE",
                   help="compare with this list instead of the one in use")
    z.add_argument("--show", type=int, default=12,
                   help="how many added/removed entries to list (default 12)")
    z.set_defaults(func=cmd_aml)

    m = sub.add_parser("rename", help="give a stored audit a different name")
    m.add_argument("name", help="project name, as shown by `projects`")
    m.add_argument("to", help="the new name")
    m.set_defaults(func=cmd_rename)

    f = sub.add_parser("forget", help="remove a stored audit (the folder is untouched)")
    f.add_argument("name", help="project name, as shown by `projects`")
    f.add_argument("--yes", action="store_true", help="actually do it")
    f.set_defaults(func=cmd_forget)

    j = sub.add_parser("projects", help="list audited jobs")
    j.add_argument("--root", metavar="NAME",
                   help="print this job's folder instead of the list")
    j.set_defaults(func=cmd_projects)

    x = sub.add_parser("vision", help="read scanned pages with a vision model")
    x.add_argument("name", help="project name (must already be audited)")
    # Taken from the registry rather than written out, so a new vision kind is
    # reachable the moment it exists. `backfill` was added as the eighth kind
    # and spent two increments unreachable from the command line.
    x.add_argument("--kind", choices=sorted(TARGETS), default="mtr",
                   help="which scanned documents to read (default: mtr)")
    x.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"one of {', '.join(sorted(MODEL_PRICES))}, or "
                        f"local:<ollama-model> to read on this machine for "
                        f"nothing (default: {DEFAULT_MODEL})")
    # Resolved after parsing, because the sensible default depends on the
    # model: a local one turns the image into vision tokens at its native
    # size, so a full-resolution page is four times the work and times out on
    # a laptop GPU. Hosted models are billed per image at a capped token
    # count and read better large.
    x.add_argument("--max-edge", type=int, default=None,
                   help=f"longest image edge in pixels (default: "
                        f"{DEFAULT_MAX_EDGE}, or {LOCAL_MAX_EDGE} for a local model)")
    x.add_argument("--limit", type=int, help="read at most this many documents")
    # The API scales any image down to roughly 1568px on its long edge, so
    # small print on a whole page is unreadable however high --max-edge is set,
    # and the model guesses rather than declining. Reading a page again as four
    # overlapping close-ups is what makes heat and weld numbers right, at five
    # reads instead of one — so 'auto' spends it on the two kinds whose
    # identifiers are matched character for character elsewhere, and leaves the
    # rest whole. The estimate above prints which before anything is sent.
    x.add_argument("--tiles", choices=TILE_MODES, default="auto",
                   help=f"read close-ups of each page as well as the whole "
                        f"page: auto = only {', '.join(sorted(TILED_KINDS))} "
                        f"(default), always = every kind (4x the cost), "
                        f"never = whole pages only")
    x.add_argument("--dry-run", action="store_true",
                   help="show the cost estimate and exit without sending anything")
    x.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    x.set_defaults(func=cmd_vision)

    o = sub.add_parser("ocr", help="read scanned certificates on this machine, free")
    o.add_argument("name", help="project name (must already be audited)")
    o.add_argument("--limit", type=int, help="read at most this many documents")
    o.add_argument("--dry-run", action="store_true",
                   help="say how many would be read, and stop")
    o.set_defaults(func=cmd_ocr)

    k = sub.add_parser("correct",
                       help="record what you read off a page, overriding the readers")
    k.add_argument("name", help="project name")
    k.add_argument("--document", help="part of the filename to correct")
    k.add_argument("--field", default="manufacturer",
                   help="which value to set (default: manufacturer)")
    k.add_argument("--value", help="what it actually says")
    k.add_argument("--note", default="", help="why, for whoever reads this later")
    k.add_argument("--all", action="store_true",
                   help="apply to every document the name matches")
    k.add_argument("--list", action="store_true", help="show what has been corrected")
    k.set_defaults(func=cmd_correct)

    n = sub.add_parser("vendor",
                       help="say which mill a misread letterhead belongs to")
    n.add_argument("name", help="project name")
    n.add_argument("--reads", help="the name as the readers spelled it")
    n.add_argument("--is", dest="is_really", help="the mill it actually is")
    n.add_argument("--note", default="", help="why, for whoever reads this later")
    n.add_argument("--accept-similar", action="store_true",
                   help="also record every unconfirmed name that resembles it")
    n.add_argument("--forget", action="store_true", help="remove an entry")
    n.add_argument("--list", action="store_true", help="show what has been identified")
    n.set_defaults(func=cmd_vendor)

    v = sub.add_parser("serve", help="run the desktop UI")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    v.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
