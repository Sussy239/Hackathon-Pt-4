"""Build per-test coverage map for sqlglot.

Runs pytest with coverage.py dynamic contexts (one context per test),
then queries the resulting SQLite .coverage database to build:

  maps/coverage_lines.json   # {source_file: {line_num: [test_numbers]}}
  maps/test_to_files.json    # {test_number: [source_files touched]}
  maps/file_to_tests.json    # {source_file: [test_numbers touching it]}

We map sqlglot source files (paths starting with sqlglot/) to test numbers
from tests.json. Tests not in tests.json (skipped) are dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def run_coverage(sqlglot_root: Path, tests_json: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cov_file = out_dir / ".coverage"

    if cov_file.exists():
        cov_file.unlink()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(sqlglot_root)
    env["SKIP_INTEGRATION"] = "0"

    env["COVERAGE_FILE"] = str(cov_file)

    cmd = [
        sys.executable, "-m", "pytest",
        "--cov=sqlglot",
        "--cov-branch",
        "--cov-context=test",
        "-p", "no:cacheprovider",
        "-p", "no:xdist",
        "--timeout=60",
        "-q",
        "--no-header",
        "tests/",
    ]
    print("Running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, cwd=str(sqlglot_root), env=env, capture_output=True, text=True)
    print(proc.stdout[-3000:], file=sys.stderr)
    print("STDERR:", proc.stderr[-3000:], file=sys.stderr)
    print("Return code:", proc.returncode, file=sys.stderr)

    if not cov_file.exists():
        raise RuntimeError(f"Coverage file not created at {cov_file}")


def extract_maps(cov_file: Path, tests_json: Path, out_dir: Path, sqlglot_root: Path) -> None:
    tests = json.loads(tests_json.read_text())["tests"]
    test_id_to_number = {t["id"]: t["number"] for t in tests}

    conn = sqlite3.connect(str(cov_file))
    cur = conn.cursor()

    tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print("Tables:", tables, file=sys.stderr)

    file_paths = dict(cur.execute("SELECT id, path FROM file").fetchall())
    contexts = dict(cur.execute("SELECT id, context FROM context").fetchall())
    print(f"Files: {len(file_paths)}, Contexts: {len(contexts)}", file=sys.stderr)

    sample = list(contexts.items())[:5]
    for cid, cname in sample:
        print(f"  context {cid}: {cname!r}", file=sys.stderr)

    coverage_lines: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    file_to_tests: dict[str, set[int]] = defaultdict(set)
    test_to_files: dict[int, set[str]] = defaultdict(set)

    def line_context_rows():
        if "line" in tables:
            try:
                return cur.execute("SELECT file_id, context_id, lineno FROM line").fetchall()
            except sqlite3.OperationalError:
                pass
        if "line_bits" in tables:
            rows = cur.execute("SELECT file_id, context_id, line_bits FROM line_bits").fetchall()
            decoded = []
            for fid, cid, bits in rows:
                if not bits:
                    continue
                for i, byte in enumerate(bits):
                    if byte == 0:
                        continue
                    for b in range(8):
                        if byte & (1 << b):
                            lineno = i * 8 + b + 1
                            decoded.append((fid, cid, lineno))
            return decoded
        raise RuntimeError("Neither 'line' nor 'line_bits' table found in coverage DB")

    rows = line_context_rows()
    print(f"Total (file, context, line) rows: {len(rows)}", file=sys.stderr)

    matched = 0
    unmatched_contexts = set()
    for fid, cid, lineno in rows:
        path = file_paths.get(fid)
        if not path:
            continue
        try:
            rel = str(Path(path).resolve().relative_to(sqlglot_root.resolve()))
        except ValueError:
            rel = path
        if not rel.startswith("sqlglot/"):
            continue
        context_name = contexts.get(cid, "")
        test_id = context_name
        if "|" in test_id:
            test_id = test_id.split("|", 1)[1]
        test_num = test_id_to_number.get(test_id)
        if test_num is None:
            test_num = test_id_to_number.get(test_id.split("[")[0])
        if test_num is None:
            unmatched_contexts.add(test_id)
            continue
        matched += 1
        coverage_lines[rel][lineno].add(test_num)
        file_to_tests[rel].add(test_num)
        test_to_files[test_num].add(rel)

    print(f"Matched rows: {matched}", file=sys.stderr)
    print(f"Unmatched test ids (sample): {list(unmatched_contexts)[:10]}", file=sys.stderr)
    print(f"Source files covered: {len(coverage_lines)}", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)

    cov_serializable = {
        f: {str(ln): sorted(nums) for ln, nums in lines.items()}
        for f, lines in coverage_lines.items()
    }
    (out_dir / "coverage_lines.json").write_text(json.dumps(cov_serializable))

    file_to_tests_ser = {f: sorted(nums) for f, nums in file_to_tests.items()}
    (out_dir / "file_to_tests.json").write_text(json.dumps(file_to_tests_ser))

    test_to_files_ser = {str(t): sorted(fs) for t, fs in test_to_files.items()}
    (out_dir / "test_to_files.json").write_text(json.dumps(test_to_files_ser))

    (out_dir / "test_ids.json").write_text(json.dumps(test_id_to_number))

    print(f"Wrote maps to {out_dir}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlglot-root", type=Path, default=Path("."))
    parser.add_argument("--tests", type=Path, required=True, help="tests.json from sample/data")
    parser.add_argument("--out", type=Path, required=True, help="output directory for maps")
    args = parser.parse_args()

    sqlglot_root = args.sqlglot_root.resolve()
    print(f"sqlglot root: {sqlglot_root}", file=sys.stderr)
    print(f"tests.json: {args.tests}", file=sys.stderr)
    print(f"out dir: {args.out}", file=sys.stderr)

    if (args.out / ".coverage").exists():
        (args.out / ".coverage").unlink()

    run_coverage(sqlglot_root, args.tests, args.out)
    extract_maps(args.out / ".coverage", args.tests, args.out, sqlglot_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
