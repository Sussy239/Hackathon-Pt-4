#!/usr/bin/env python3
"""Select and schedule sqlglot tests for each patch in /data/commits."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def parse_patch(patch_text: str) -> tuple[set[str], set[str]]:
    """Return (modified_files, touched_identifiers) from a unified diff."""
    modified_files: set[str] = set()
    identifiers: set[str] = set()

    for line in patch_text.splitlines():
        m_file = DIFF_FILE_RE.match(line)
        if m_file:
            path = m_file.group(1).strip()
            if path != "/dev/null":
                modified_files.add(path)
            continue

        m_hunk = HUNK_RE.match(line)
        if m_hunk and m_hunk.group(1):
            for tok in IDENT_RE.findall(m_hunk.group(1)):
                identifiers.add(tok.lower())

        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            content = line[1:]
            for tok in IDENT_RE.findall(content):
                identifiers.add(tok.lower())

    return modified_files, identifiers


def schedule_lpt(
    test_nums: list[int],
    dur_ms_by_num: dict[int, int],
    max_workers: int = 8,
) -> list[list[int]]:
    """Choose worker count k in 1..min(8, N) minimizing max_load_ms + 2000 * k."""
    if not test_nums:
        return []

    ordered = sorted(test_nums, key=lambda n: (-dur_ms_by_num.get(n, 0), n))
    upper_k = min(max_workers, len(ordered))

    best_cost: int | None = None
    best_workers: list[list[int]] = []

    for k in range(1, upper_k + 1):
        buckets: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for n in ordered:
            idx = min(range(k), key=lambda i: (loads[i], i))
            buckets[idx].append(n)
            loads[idx] += dur_ms_by_num.get(n, 0)

        cost = max(loads) + 2000 * k
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = [b for b in buckets if b]

    return best_workers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()

    tests_doc = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))
    tests_list = tests_doc.get("tests", [])

    dur_ms_by_num: dict[int, int] = {}
    tests_by_file: dict[str, list[int]] = defaultdict(list)
    test_id_tokens: list[tuple[int, set[str]]] = []

    for entry in tests_list:
        num = int(entry["number"])
        t_file = entry["file"]
        sec = float(entry.get("seconds", 0.0))
        dur_ms_by_num[num] = int(round(sec * 1000))
        tests_by_file[t_file].append(num)

        tokens = {
            tok.lower()
            for tok in re.split(r"[^A-Za-z0-9_]+", entry.get("id", ""))
            if len(tok) >= 3
        }
        test_id_tokens.append((num, tokens))

    index_doc = json.loads(args.index.read_text(encoding="utf-8"))
    trans_deps: dict[str, list[str]] = index_doc.get("transitive_dependents", {})
    symbol_to_files: dict[str, list[str]] = index_doc.get("symbol_to_files", {})

    commits_dir = args.data_dir / "commits"
    patch_paths = sorted(commits_dir.glob("*.patch"))

    for patch_path in patch_paths:
        commit_id = patch_path.stem
        mod_files, touched_ids = parse_patch(
            patch_path.read_text(encoding="utf-8", errors="replace")
        )

        candidate_files: set[str] = set()
        for mf in mod_files:
            if mf.startswith("tests/") and mf.endswith(".py"):
                candidate_files.add(mf)
            candidate_files.add(mf)
            for dep in trans_deps.get(mf, ()):
                candidate_files.add(dep)

        # Map modified sqlglot modules to likely test files (e.g. sqlglot/dialects/postgres.py -> tests/dialects/test_postgres.py)
        for mf in list(candidate_files):
            if mf.startswith("sqlglot/") and mf.endswith(".py"):
                p = Path(mf)
                stem = p.stem
                if stem != "__init__":
                    sub = Path(*p.parts[1:-1]) if len(p.parts) > 2 else Path(".")
                    guess_test = (Path("tests") / sub / f"test_{stem}.py").as_posix()
                    if guess_test in tests_by_file:
                        candidate_files.add(guess_test)

        # Symbol-to-file matches
        for sym in touched_ids:
            for f in symbol_to_files.get(sym, ()):
                candidate_files.add(f)

        selected_set: set[int] = set()
        for cf in candidate_files:
            selected_set.update(tests_by_file.get(cf, ()))

        # Direct test ID / dialect / symbol matching when candidate files don't already cover it
        if touched_ids and not selected_set:
            for num, t_toks in test_id_tokens:
                if touched_ids & t_toks:
                    selected_set.add(num)

        workers = schedule_lpt(sorted(selected_set), dur_ms_by_num, max_workers=8)
        line = json.dumps({"commit": commit_id, "workers": workers}, separators=(",", ":"))
        sys.stdout.write(line + "\n")

    sys.stdout.flush()


if __name__ == "__main__":
    main()