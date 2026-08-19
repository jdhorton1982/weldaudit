"""Read the hand-checked pages with a given model and score what comes back.

    python -m eval.score --model local:qwen2.5vl:7b
    python -m eval.score --model claude-haiku-4-5
    python -m eval.score --model local:qwen2.5vl:7b --kind reader_sheet

Two numbers matter and they are reported separately. **Accuracy** is how much
of the page a model got right. **Safety** is what it did with the handful of
fields that decide a finding — a wrong ambient temperature costs nothing, a
wrong accept/reject is the whole audit.

Wrong is also split from blank, deliberately. A model that returns null where
it cannot read is behaving as the prompts ask and the page can be flagged for a
human; a model that invents a plausible value is the dangerous one, because
nothing downstream can tell.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.ground_truth import CRITICAL, PAGES            # noqa: E402
from weldaudit.db import Database                        # noqa: E402
from weldaudit.pipeline import default_db_path           # noqa: E402
from weldaudit.vision import (  # noqa: E402
    TILE_MODES, Estimate, VisionReader, credentials_available, is_local,
    local_available, tiles_for,
)


def _norm(value):
    """Compare on meaning, not on punctuation."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    for junk in ('"', "”", "“", "'", "’", " ", ",", ".", "-", "_", "/", "#", ":"):
        text = text.replace(junk, "")
    return text or None


#: Fields holding a company name. Scored the way the audit consumes them —
#: through `normalise_manufacturer` and a fuzzy AML lookup — because
#: "DAEKWANG BEND CO., LTD.(DKB)" and "Daekwang Bend Co Ltd" are the same
#: answer to the only question anything downstream asks. Marking one of them
#: wrong measures transcription, which is not what this tool needs to be right
#: about.
_NAME_FIELDS = frozenset({"issuing_company", "mill_name", "manufacturer"})

#: token_set_ratio over the normalised names. High enough that "Kandal Pipe"
#: and "Model Pipe" stay different companies, loose enough that punctuation,
#: legal forms and a stray plural do not.
_SAME_COMPANY = 90


def _same_company(want, got) -> bool:
    from rapidfuzz import fuzz

    from weldaudit.aml import normalise_manufacturer

    a, b = normalise_manufacturer(str(want)), normalise_manufacturer(str(got))
    if not a or not b:
        return a == b
    return fuzz.token_set_ratio(a, b) >= _SAME_COMPANY


def _matches(field: str, want, got) -> bool:
    leaf = field.rsplit(".", 1)[-1].removesuffix("[]")
    if leaf in _NAME_FIELDS and want is not None and got is not None:
        return _same_company(want, got)
    return _norm(want) == _norm(got)


def _walk(expected, got, path=""):
    """Yield (field, expected, got) for every leaf in the expected payload."""
    if isinstance(expected, dict):
        for key, want in expected.items():
            if key.startswith("_"):
                continue
            child = got.get(key) if isinstance(got, dict) else None
            yield from _walk(want, child, f"{path}.{key}" if path else key)
    elif isinstance(expected, list):
        listed = got if isinstance(got, list) else []
        for i, want in enumerate(expected):
            child = listed[i] if i < len(listed) else None
            yield from _walk(want, child, f"{path}[]")
    else:
        yield path, expected, got


def score_page(entry: dict, payload: dict) -> dict:
    critical = set(CRITICAL.get(entry["kind"], ()))
    tally = {"right": 0, "blank": 0, "wrong": 0,
             "crit_right": 0, "crit_blank": 0, "crit_wrong": 0}
    misses: list[tuple[str, object, object]] = []
    for field, want, got in _walk(entry["expected"], payload):
        is_crit = field in critical
        if _matches(field, want, got):
            outcome = "right"
        elif _norm(got) is None:
            outcome = "blank"
        else:
            outcome = "wrong"
        tally[outcome] += 1
        if is_crit:
            tally[f"crit_{outcome}"] += 1
        if outcome != "right" and (is_crit or len(misses) < 6):
            misses.append((field, want, got))
    tally["misses"] = misses
    return tally


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--kind", help="score only this document kind")
    ap.add_argument("--max-edge", type=int, default=2000)
    ap.add_argument("--tiles", choices=TILE_MODES, default="auto",
                    help="auto = the kinds that need it, always = every kind, "
                         "never = whole pages only")
    ap.add_argument("--db", default=None,
                    help="the audit database, read for document paths")
    # Benchmark readings are kept away from the audit database rather than in
    # it. They used to share one cache, and scoring the local model overwrote
    # Haiku's readings as the ones a re-audit replayed — a free benchmark
    # quietly downgraded the findings on a real project. Repeat scoring is
    # still free, because this file is a cache too.
    ap.add_argument("--cache-db", default=None,
                    help="where to cache benchmark readings "
                         "(default: eval-cache.db beside the audit database)")
    ap.add_argument("--no-cache", action="store_true",
                    help="read every page afresh and store nothing - for "
                         "measuring how much a model varies run to run")
    args = ap.parse_args()

    if is_local(args.model):
        ok, served = local_available()
        if not ok:
            print(f"Cannot reach a local model: {served}")
            print("Install Ollama, then: ollama pull qwen2.5vl:7b")
            return 2
        print(f"Local daemon serving: {served}\n")
    elif not credentials_available():
        # Checked once rather than discovered eleven times: without this the
        # run reports the same auth failure per page and buries the cause.
        # Two estimates, because the tiled kinds cost five reads and the
        # rest cost one; a single figure would be wrong either way.
        tiled = [p for p in PAGES if tiles_for(args.tiles, p["kind"])]
        est = Estimate(documents=len(tiled), pages=len(tiled), model=args.model,
                       max_edge=args.max_edge, tiles=True)
        rest = Estimate(documents=len(PAGES) - len(tiled),
                        pages=len(PAGES) - len(tiled), model=args.model,
                        max_edge=args.max_edge, tiles=False)
        print(f"No Anthropic credentials on this machine, so nothing was sent.")
        print(f"  Scoring {args.model} on these {len(PAGES)} pages would cost "
              f"about ${est.cost_usd + rest.cost_usd:,.2f}.")
        print("  set ANTHROPIC_API_KEY=sk-ant-...   then run this again,")
        print("  or score the local model for nothing: --model local:qwen2.5vl:7b")
        return 2

    audit_db = Path(args.db or default_db_path())
    raw = sqlite3.connect(audit_db)
    raw.row_factory = sqlite3.Row
    # The reader touches its database only to cache, so pointing it elsewhere
    # is all the isolation this needs.
    cache_path = Path(args.cache_db) if args.cache_db else (
        audit_db.parent / "eval-cache.db")
    if args.no_cache:
        cache_path = Path(tempfile.mkdtemp(prefix="weldaudit-eval-")) / "throwaway.db"
    db = Database(cache_path)
    kept = "" if not args.no_cache else " (thrown away at the end)"
    print(f"Caching readings in {cache_path}{kept}\n")
    reader = VisionReader(db, model=args.model, max_edge=args.max_edge,
                          tiles=args.tiles)

    pages = [p for p in PAGES if not args.kind or p["kind"] == args.kind]
    totals = {k: 0 for k in ("right", "blank", "wrong",
                             "crit_right", "crit_blank", "crit_wrong")}
    seconds = 0.0
    print(f"{'page':46} {'kind':18} {'fields':>14} {'critical':>14}   time")
    for entry in pages:
        row = raw.execute(
            """SELECT d.path, d.fingerprint, d.id FROM document d
               JOIN project p ON p.id = d.project_id
               WHERE p.name=? AND d.filename=? ORDER BY LENGTH(d.path) LIMIT 1""",
            (entry["project"], entry["document"]),
        ).fetchone()
        if row is None:
            print(f"{entry['document'][:44]:46} -- not indexed; run an audit first")
            continue
        fingerprint = row["fingerprint"] or str(row["id"])
        began = time.time()
        try:
            payload = reader.read_page(row["path"], entry["page"],
                                       entry["kind"], fingerprint)
        except Exception as why:                     # noqa: BLE001
            print(f"{entry['document'][:44]:46} -- {type(why).__name__}: {why}")
            continue
        took = time.time() - began
        seconds += took
        if payload.get("_error"):
            print(f"{entry['document'][:44]:46} -- {payload['_error']}")
            continue

        tally = score_page(entry, payload)
        for k in totals:
            totals[k] += tally[k]
        n = tally["right"] + tally["blank"] + tally["wrong"]
        cn = tally["crit_right"] + tally["crit_blank"] + tally["crit_wrong"]
        print(f"{entry['document'][:44]:46} {entry['kind']:18} "
              f"{tally['right']:5}/{n:<8} "
              f"{tally['crit_right']:5}/{cn:<8} {took:5.1f}s")
        for field, want, got in tally["misses"]:
            mark = "!!" if field in CRITICAL.get(entry["kind"], ()) else "  "
            print(f"      {mark} {field:38} want {want!r:28} got {got!r}")

    n = sum(totals[k] for k in ("right", "blank", "wrong"))
    cn = sum(totals[k] for k in ("crit_right", "crit_blank", "crit_wrong"))
    if not n:
        return 1
    print()
    print(f"  every field    {totals['right']}/{n} right "
          f"({totals['right'] / n:.0%}), {totals['blank']} blank, "
          f"{totals['wrong']} wrong")
    print(f"  finding-critical {totals['crit_right']}/{cn} right "
          f"({totals['crit_right'] / max(cn, 1):.0%}), "
          f"{totals['crit_blank']} blank, {totals['crit_wrong']} WRONG")
    print(f"  {seconds:.0f}s for {len(pages)} pages "
          f"({seconds / max(len(pages), 1):.1f}s each)")
    print()
    print("  A blank critical field is a page to flag for a human. A wrong one")
    print("  is a finding that silently disappears — that is the number to")
    print("  judge a model on.")
    if args.no_cache:
        shutil.rmtree(cache_path.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
