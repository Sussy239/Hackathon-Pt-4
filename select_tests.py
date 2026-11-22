#!/usr/bin/env python3
"""Select and schedule sqlglot tests for each patch in /data/commits."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
HUNK_RE = re.compile(r"^@@ .*? @@(.*)$")
DIFF_PATH_RE = re.compile(r"^(?:\+\+\+|---) ([ab]/.+|/dev/null)(?:\s.*)?$")


def parse_patch(patch_text: str) -> tuple[set[str], set[str]]:
    """Return changed repository paths and identifiers found in the diff."""
    changed_files: set[str] = set()
    identifiers: set[str] = set()

    for line in patch_text.splitlines():
        path_match = DIFF_PATH_RE.match(line)
        if path_match:
            path = path_match.group(1)
            if path != "/dev/null":
                changed_files.add(path[2:])
            continue

        hunk_match = HUNK_RE.match(line)
        if hunk_match:
            identifiers.update(token.lower() for token in IDENT_RE.findall(hunk_match.group(1)))

        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            identifiers.update(token.lower() for token in IDENT_RE.findall(line[1:]))

    # Include filename terms as useful clues, especially for small edits.
    for path in changed_files:
        identifiers.update(token.lower() for token in IDENT_RE.findall(path))

    return changed_files, identifiers


def schedule_lpt(
    test_nums: list[int],
    durations_ms: dict[int, int],
    max_workers: int = 8,
) -> list[list[int]]:
    """Use LPT scheduling and choose the worker count with the lowest estimated cost."""
    if not test_nums:
        return []

    ordered = sorted(test_nums, key=lambda n: (-durations_ms.get(n, 0), n))
    best_cost: int | None = None
    best_workers: list[list[int]] = []

    for worker_count in range(1, min(max_workers, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(worker_count)]
        loads = [0] * worker_count

        for test_num in ordered:
            worker = min(range(worker_count), key=lambda i: (loads[i], i))
            workers[worker].append(test_num)
            loads[worker] += durations_ms.get(test_num, 0)

        cost = max(loads) + 2000 * worker_count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = [worker for worker in workers if worker]

    return best_workers


def test_paths_for_source(source: str, known_test_files: set[str]) -> set[str]:
    """Guess corresponding test paths for a changed Python source file."""
    path = Path(source)
    if not source.startswith("sqlglot/") or path.suffix != ".py":
        return set()

    stem = path.stem
    if stem == "__init__":
        return set()

    relative_parent = Path(*path.parts[1:-1]) if len(path.parts) > 2 else Path()
    guess = (Path("tests") / relative_parent / f"test_{stem}.py").as_posix()
    return {guess} if guess in known_test_files else set()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()

    tests_doc = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))
    test_entries = tests_doc.get("tests", [])

    durations_ms: dict[int, int] = {}
    tests_by_file: dict[str, list[int]] = defaultdict(list)
    test_tokens: list[tuple[int, set[str]]] = []

    for entry in test_entries:
        number = int(entry["number"])
        file_path = entry["file"]
        durations_ms[number] = int(round(float(entry.get("seconds", 0.0)) * 1000))
        tests_by_file[file_path].append(number)

        tokens = {
            token.lower()
            for token in IDENT_RE.findall(entry.get("id", ""))
        }
        test_tokens.append((number, tokens))

    all_test_numbers = set(durations_ms)
    known_test_files = set(tests_by_file)

    index_doc = json.loads(args.index.read_text(encoding="utf-8"))
    transitive_dependents: dict[str, list[str]] = index_doc.get(
        "transitive_dependents", {}
    )
    symbol_to_files: dict[str, list[str]] = index_doc.get("symbol_to_files", {})

    commits_dir = args.data_dir / "commits"

    for patch_path in sorted(commits_dir.glob("*.patch")):
        commit_id = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        changed_files, touched_identifiers = parse_patch(patch_text)

        candidate_files: set[str] = set()

        for changed_file in changed_files:
            candidate_files.add(changed_file)
            candidate_files.update(transitive_dependents.get(changed_file, ()))
            candidate_files.update(test_paths_for_source(changed_file, known_test_files))

            # A directly changed test file should be selected as-is.
            if changed_file in known_test_files:
                candidate_files.add(changed_file)

        # Select tests associated with affected files and files defining touched symbols.
        selected_tests: set[int] = set()
        for candidate_file in candidate_files:
            selected_tests.update(tests_by_file.get(candidate_file, ()))

        for identifier in touched_identifiers:
            for source_file in symbol_to_files.get(identifier, ()):
                selected_tests.update(tests_by_file.get(source_file, ()))
                for dependent in transitive_dependents.get(source_file, ()):
                    selected_tests.update(tests_by_file.get(dependent, ()))
                for test_file in test_paths_for_source(source_file, known_test_files):
                    selected_tests.update(tests_by_file.get(test_file, ()))

        # Always perform identifier matching, even if file-based selection found tests.
        for number, tokens in test_tokens:
            if touched_identifiers.intersection(tokens):
                selected_tests.add(number)

        # A patch with no detectable test targets gets the conservative full-suite fallback.
        if not selected_tests:
            selected_tests = set(all_test_numbers)

        workers = schedule_lpt(sorted(selected_tests), durations_ms)
        output = {"commit": commit_id, "workers": workers}
        sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")

    sys.stdout.flush()


if __name__ == "__main__":
    main()
