"""Refuse a commit that would publish a secret, a binary, or a customer's data.

This repository is public and the work is done against confidential turnover
packages, so every commit is a chance to publish something that cannot be
taken back. Reviewing the diff by hand caught it four times and missed it
twice — once when a code comment quoted a real certificate, and once when a
.docx holding a database password was swept in by ``git add -A``. That second
one is the reason this exists rather than a checklist: a scan that reads the
text diff is structurally blind to a binary, and no amount of care fixes that.

Three checks, on the *staged* content only:

1. **New binaries** are refused unless the path is one the project already
   ships. A binary carries its contents past any text search.
2. **Secrets** — connection strings with a password in them, API keys, private
   key blocks.
3. **A customer's names**, read from ``private/forbidden.txt`` if it exists.
   That list is deliberately not in this file: writing the operator, the job
   names and the mills into a committed script would publish exactly what the
   check is for. One pattern per line, ``#`` for comments.

``git commit --no-verify`` still goes through. That is on purpose — the point
is to make publishing a secret take a deliberate act rather than an ordinary
one — but it prints loudly first.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Binaries the project legitimately ships. Anything else has to be added here
#: on purpose, which is the whole point: a file nobody chose is a file nobody
#: has read.
ALLOWED_BINARIES = {
    "weldaudit.ico",
}
ALLOWED_BINARY_SUFFIXES = {".svg"}          # text really, but git may call it binary

#: Things that are a secret wherever they appear.
SECRETS = [
    (r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@", "a connection string with a password in it"),
    (r"\bsk-ant-[A-Za-z0-9_-]{8,}", "an Anthropic API key"),
    (r"\bsb_secret_[A-Za-z0-9_-]{8,}", "a Supabase secret key"),
    (r"\bsb_publishable_[A-Za-z0-9_-]{8,}", "a Supabase publishable key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "a private key"),
    (r"\bghp_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
     "a hard-coded credential"),
]

#: Anchored to this file rather than to the working directory. A git hook is
#: not promised a particular cwd, and anyone running the check by hand runs it
#: from wherever they happen to be. Relative, it resolved to nothing from a
#: subdirectory, :func:`site_patterns` returned no patterns at all, and the
#: check passed everything having never consulted the site list - a guard that
#: quietly becomes a no-op, which is worse than not having one.
FORBIDDEN_LIST = Path(__file__).resolve().parent.parent / "private" / "forbidden.txt"


def staged() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def binaries() -> list[str]:
    """Staged paths git reports as binary (numstat shows '-' for both counts)."""
    out = subprocess.run(["git", "diff", "--cached", "--numstat", "--diff-filter=ACMR"],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "-" and parts[1] == "-":
            found.append(parts[2])
    return found


def contents(path: str) -> str:
    out = subprocess.run(["git", "show", f":{path}"],
                         capture_output=True, text=True, errors="ignore")
    return out.stdout


def site_patterns() -> list[tuple[re.Pattern, str]]:
    """The names that must not be published, or nothing - said out loud.

    No list is a legitimate state: it is gitignored, so a fresh clone has none
    and there is nothing to check against. What is not legitimate is finding
    that out silently. This check passing means one of two very different
    things, and the difference is worth one line on stderr.
    """
    if not FORBIDDEN_LIST.is_file():
        print(f"note: no {FORBIDDEN_LIST.name} beside this check, so staged files "
              f"were not compared against any customer's names.", file=sys.stderr)
        return []
    out = []
    for line in FORBIDDEN_LIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append((re.compile(line, re.IGNORECASE), "a customer's data"))
        except re.error:
            out.append((re.compile(re.escape(line), re.IGNORECASE), "a customer's data"))
    return out


def main() -> int:
    complaints: list[str] = []

    for path in binaries():
        name = Path(path).name
        if name in ALLOWED_BINARIES or Path(path).suffix.lower() in ALLOWED_BINARY_SUFFIXES:
            continue
        complaints.append(
            f"  {path}\n"
            f"      a binary nobody chose to ship. Its contents cannot be read by\n"
            f"      any diff, so it cannot be reviewed before it is published.")

    rules = [(re.compile(p), why) for p, why in SECRETS] + site_patterns()
    for path in staged():
        if path in binaries():
            continue
        text = contents(path)
        if not text:
            continue
        for pattern, why in rules:
            hit = pattern.search(text)
            if hit:
                shown = hit.group(0)
                if len(shown) > 24:
                    shown = shown[:12] + "..." + shown[-6:]   # not an ellipsis: a Windows console mangles it
                complaints.append(f"  {path}\n      {why}: {shown!r}")
                break

    if not complaints:
        return 0

    print("\nThis commit was stopped before it published something.\n", file=sys.stderr)
    for c in complaints:
        print(c, file=sys.stderr)
    print(
        "\nThe repository is public and cannot be un-published: a force-push\n"
        "removes a file from the branch but not from anyone who already has it,\n"
        "and a credential that reached a public commit has to be rotated whatever\n"
        "happens next.\n\n"
        "  unstage it     git restore --staged <path>\n"
        "  ignore it      add the path to .gitignore\n"
        "  it is fine     git commit --no-verify\n",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
