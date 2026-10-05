"""Optional runtime signal: which tests executed which symbols, from one per-test coverage run.

Collect it with pytest-cov (`pytest --cov=<pkg> --cov-context=test`, or `changelens coverage --run "..."`), then
import the .coverage file. Tests that ran the changed code are then ranked first and marked `covered`. Coverage is
mapped to symbols, not line numbers, so it survives later edits that shift lines.
"""
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from .index import git, state_dir

COVERAGE_ARGS = ["--cov", "--cov-context=test", "--cov-report="]


def _numbits(blob):
    """coverage.py's line bitmap: bit j of byte i set -> line i*8+j executed."""
    return {i * 8 + j for i, byte in enumerate(blob) if byte for j in range(8) if byte >> j & 1}


def _relpath(index, path):
    """Absolute path recorded by coverage (maybe on another machine/checkout) -> indexed repo-relative path."""
    p = path.replace("\\", "/")
    try:
        rel = Path(path).resolve().relative_to(index.root.resolve()).as_posix()
        if rel in index.by_file:
            return rel
    except ValueError:
        pass
    return max((f for f in index.by_file if p.endswith("/" + f)), key=len, default=None)


def import_coverage(index, path):
    """Read a .coverage SQLite file into {symbol: sorted test ids} and save it with the repo's ChangeLens state."""
    db = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    contexts = {i: re.sub(r"\[.*$", "", c.split("|")[0]) for i, c in db.execute("select id, context from context") if c}
    if not contexts:
        raise ValueError(f"{path} has no per-test contexts: collect it with --cov-context=test "
                         "(on Python 3.12+, also set COVERAGE_CORE=ctrace)")
    files = {i: _relpath(index, p) for i, p in db.execute("select id, path from file")}
    hits = {}
    for fid, cid, bits in db.execute("select file_id, context_id, numbits from line_bits"):
        hits.setdefault((fid, cid), set()).update(_numbits(bits))
    for fid, cid, a, b in db.execute("select file_id, context_id, fromno, tono from arc"):  # branch coverage
        hits.setdefault((fid, cid), set()).update(x for x in (a, b) if x > 0)
    db.close()

    symbols, at = {}, {}
    for (fid, cid), lines in hits.items():
        f, test = files.get(fid), contexts.get(cid)
        if not f or not test:
            continue
        for line in lines:
            if (f, line) not in at:
                at[f, line] = [s.name for s in index.symbols_at(f, line) if s.kind in ("function", "class")]
            for s in at[f, line]:
                symbols.setdefault(s, set()).add(test)
    data = {"commit": git(index.root, "rev-parse", "HEAD").strip(), "tests": len(set(contexts.values())),
            "symbols": {s: sorted(t) for s, t in sorted(symbols.items())}}
    out = state_dir(index.root) / "coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    index.coverage = {s: set(t) for s, t in data["symbols"].items()}
    return data


def collect(root, test_cmd, timeout=3600):
    """Run the test suite once with per-test coverage; returns the .coverage path."""
    from .bench import _split
    args = _split(test_cmd)
    if (Path(root) / args[0]).is_file():
        args[0] = str(Path(root) / args[0])
    env = {**os.environ, "COVERAGE_CORE": "ctrace"}  # sys.monitoring (3.12+ default) can't record test contexts
    subprocess.run(args + COVERAGE_ARGS, cwd=root, env=env, capture_output=True, timeout=timeout)
    path = Path(root) / ".coverage"
    if not path.exists():
        raise ValueError("no .coverage written: is pytest-cov installed in the test environment?")
    return path


def clear(root):
    (state_dir(root) / "coverage.json").unlink(missing_ok=True)
