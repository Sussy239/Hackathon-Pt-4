#!/usr/bin/env python3
"""Build a lightweight static import & symbol index for sqlglot at build time."""

import argparse
import ast
import json
from collections import defaultdict, deque
from pathlib import Path


def parse_imports(py_path: Path, repo_root: Path) -> set[str]:
    """Return repo-relative module paths (e.g. 'sqlglot/parser.py') imported by py_path."""
    imported_files: set[str] = set()
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return imported_files

    rel_parts = list(py_path.relative_to(repo_root).with_suffix("").parts)
    pkg_parts = rel_parts[:-1] if py_path.name == "__init__.py" else rel_parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                cand = repo_root / Path(*alias.name.split(".")).with_suffix(".py")
                cand_init = repo_root / Path(*alias.name.split(".")) / "__init__.py"
                if cand.is_file():
                    imported_files.add(cand.relative_to(repo_root).as_posix())
                elif cand_init.is_file():
                    imported_files.add(cand_init.relative_to(repo_root).as_posix())
        elif isinstance(node, ast.ImportFrom):
            base_parts: list[str] = []
            if node.level and node.level > 0:
                up = max(0, len(pkg_parts) - (node.level - 1))
                base_parts.extend(pkg_parts[:up])
            if node.module:
                base_parts.extend(node.module.split("."))

            if base_parts:
                mod_file = repo_root / Path(*base_parts).with_suffix(".py")
                mod_init = repo_root / Path(*base_parts) / "__init__.py"
                if mod_file.is_file():
                    imported_files.add(mod_file.relative_to(repo_root).as_posix())
                if mod_init.is_file():
                    imported_files.add(mod_init.relative_to(repo_root).as_posix())

            for alias in node.names:
                sub_parts = base_parts + alias.name.split(".")
                sub_file = repo_root / Path(*sub_parts).with_suffix(".py")
                sub_init = repo_root / Path(*sub_parts) / "__init__.py"
                if sub_file.is_file():
                    imported_files.add(sub_file.relative_to(repo_root).as_posix())
                elif sub_init.is_file():
                    imported_files.add(sub_init.relative_to(repo_root).as_posix())

    return imported_files


def extract_symbols(py_path: Path) -> set[str]:
    """Extract top-level class/function names and lowercase tokens."""
    syms: set[str] = set()
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return syms

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if len(node.name) >= 3:
                syms.add(node.name.lower())
    return syms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    repo_root = args.repo.resolve()
    py_files = sorted(
        p.relative_to(repo_root).as_posix()
        for p in repo_root.rglob("*.py")
    )

    imports_map: dict[str, list[str]] = {}
    reverse_deps: dict[str, set[str]] = defaultdict(set)
    file_symbols: dict[str, list[str]] = {}
    symbol_to_files: dict[str, set[str]] = defaultdict(set)

    for rel in py_files:
        full = repo_root / rel
        imps = sorted(parse_imports(full, repo_root))
        imports_map[rel] = imps
        for target in imps:
            reverse_deps[target].add(rel)

        syms = sorted(extract_symbols(full))
        file_symbols[rel] = syms
        for s in syms:
            symbol_to_files[s].add(rel)

    # Precompute transitive dependents (BFS up to depth 4) for each file
    transitive_dependents: dict[str, list[str]] = {}
    for rel in py_files:
        visited: set[str] = set()
        q: deque[tuple[str, int]] = deque([(rel, 0)])
        while q:
            cur, depth = q.popleft()
            if depth >= 4:
                continue
            for nxt in reverse_deps.get(cur, ()):
                if nxt not in visited and nxt != rel:
                    visited.add(nxt)
                    q.append((nxt, depth + 1))
        transitive_dependents[rel] = sorted(visited)

    payload = {
        "imports": imports_map,
        "transitive_dependents": transitive_dependents,
        "file_symbols": file_symbols,
        "symbol_to_files": {k: sorted(v) for k, v in symbol_to_files.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()