#!/usr/bin/env python3
"""Privacy scan: refuse to publish personal data.

Scans every git-tracked file (plus anything staged) for patterns that must
never reach the public repo: home-directory paths, tailnet hostnames, secret
material, and any extra patterns listed in .privacy-patterns.local (one regex
per line; that file is gitignored and never committed, so private strings can
be matched without being written down here).

Exit code 0 = clean, 1 = findings. Wired into .git/hooks/pre-push by
tools/install_hooks.sh so a push cannot happen over findings.

Run it directly:  python3 tools/privacy_scan.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (name, compiled regex, files exempt from this one rule)
RULES = [
    ("home directory path", re.compile(r"/Users/[A-Za-z]"), set()),
    ("tailnet hostname", re.compile(r"[A-Za-z0-9-]+\.tail[0-9a-f]*\.ts\.net|[A-Za-z0-9-]+\.ts\.net"), set()),
    ("private key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), set()),
    ("anthropic api key", re.compile(r"sk-ant-[A-Za-z0-9-]{10,}"), set()),
    ("plaid access token", re.compile(r"access-(sandbox|production)-[0-9a-f-]{10,}"), set()),
    ("plaid client id/secret shape", re.compile(r"\b[0-9a-f]{24}\b"), {"backend/uv.lock"}),
    ("filled secret assignment",
     re.compile(r"^(PLAID_SECRET|PLAID_CLIENT_ID|CLAUDE_API_KEY|SESSION_SECRET|TALLY_ENCRYPTION_KEY)=(?!\s*$)(?!change-me)(?!test-)", re.M),
     {".env.example"}),
]

BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
               ".ttf", ".otf", ".pdf", ".zip", ".db", ".sqlite"}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=REPO, capture_output=True, text=True,
                            check=True).stdout.splitlines()
    return sorted(set(out) | {s for s in staged if (REPO / s).exists()})


def _local_rules():
    extra = REPO / ".privacy-patterns.local"
    if not extra.exists():
        return
    for line in extra.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield (f"local pattern {line[:20]}…", re.compile(line, re.I), set())


def main() -> int:
    rules = RULES + list(_local_rules())
    findings: list[str] = []
    for rel in _tracked_files():
        path = REPO / rel
        if path.suffix.lower() in BINARY_EXTS or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, rx, exempt in rules:
            if rel in exempt:
                continue
            for m in rx.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append(f"{rel}:{line_no}: {name}")
    if findings:
        print("PRIVACY SCAN FAILED - do not push:", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"privacy scan clean ({len(_tracked_files())} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
