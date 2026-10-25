#!/usr/bin/env python3
# issue #188: CLAIM lint — capability claims in docstrings/comments must carry
# "# CLAIM: verified by tests/test_x.py" tags referencing real test files.
# CI fails on: dangling CLAIM reference, or engine/workflow.py dropping below
# the minimum CLAIM coverage for its headline claims.

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "fusion_cowork"

CLAIM_RE = re.compile(r"#?\s*CLAIM:\s*verified by\s+(.+)")
TEST_FILE_RE = re.compile(r"(tests/[\w./-]+\.py)")
MIN_CLAIMS = {"fusion_cowork/engine/workflow.py": 2}


def main() -> int:
    errors = []
    total_claims = 0
    per_file_counts = {}
    for py in sorted(SRC_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        text = py.read_text(encoding="utf-8")
        matches = CLAIM_RE.findall(text)
        per_file_counts[rel] = len(matches)
        for m in matches:
            total_claims += 1
            refs = TEST_FILE_RE.findall(m)
            if not refs:
                errors.append(f"{rel}: CLAIM tag without a tests/... reference: {m.strip()!r}")
                continue
            for ref in refs:
                if not (REPO_ROOT / ref).is_file():
                    errors.append(f"{rel}: CLAIM references missing test file: {ref}")
    for rel, minimum in MIN_CLAIMS.items():
        count = per_file_counts.get(rel, 0)
        if count < minimum:
            errors.append(f"{rel}: only {count} CLAIM tags, minimum required is {minimum}")
    if errors:
        print("CLAIM lint failed:")
        for e in errors:
            print(f"  - {e}")
        return 1
    tagged_files = sum(1 for c in per_file_counts.values() if c > 0)
    print(f"CLAIM lint passed: {total_claims} tags across {tagged_files} tagged files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
