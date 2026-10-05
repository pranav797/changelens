"""Mutation benchmark (plan §8): make one function raise, run the real test suite, and score how much of what
actually failed each predictor's test list contains."""
import ast
import os
import random
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .analyze import analyze, git_diff
from .index import Index, git, is_test

MUTANT = 'raise RuntimeError("changelens mutant")'
SUMMARY_RE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)
KS = (5, 10)


def norm(test_id):
    """Node id at test-function level: forward slashes, no parametrize suffix."""
    # cut at the first "[": parametrize ids can contain spaces, which also cut the summary line short
    return re.sub(r"\[.*$", "", test_id.strip().replace("\\", "/"))


def _split(cmd):
    if isinstance(cmd, (list, tuple)):
        return list(cmd)
    return [t.strip('"') for t in shlex.split(cmd, posix=os.name != "nt")]


def _pytest(root, cmd, extra, timeout):
    args = _split(cmd)
    if (root / args[0]).is_file():  # e.g. .venv/Scripts/python.exe: Windows won't resolve it against cwd
        args[0] = str(root / args[0])
    try:
        return subprocess.run(args + extra, cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return None


def failing(root, cmd, timeout):
    """Failing/erroring test ids (collection errors come back as file paths), or None on timeout."""
    out = _pytest(root, cmd, ["-q", "--tb=no", "-rfE", "-p", "no:cacheprovider", "--continue-on-collection-errors"],
                  timeout)
    return None if out is None else {norm(m.group(1)) for m in SUMMARY_RE.finditer(out)}


def collect(root, cmd, timeout):
    out = _pytest(root, cmd, ["-q", "--collect-only", "-p", "no:cacheprovider"], timeout) or ""
    return {norm(line) for line in out.splitlines() if "::" in line}


def mutate(root, sym):
    """Make `sym` raise on entry. Returns the file's original bytes, or None if it can't be mutated cleanly."""
    path = root / sym.file
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
        tree = ast.parse(original)
    except (UnicodeDecodeError, SyntaxError):
        return None
    name = sym.name.rsplit(".", 1)[-1]
    node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
                 and min([d.lineno for d in n.decorator_list] + [n.lineno]) == sym.start), None)
    if node is None or node.body[0].lineno == node.lineno:  # not found, or a one-line `def f(): ...`
        return None
    lines = text.splitlines(keepends=True)
    first = node.body[0]
    has_doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
    anchor = node.body[1] if has_doc and len(node.body) > 1 else None if has_doc else first
    ref = anchor or first
    indent = re.match(r"[ \t]*", lines[ref.lineno - 1]).group()
    at = anchor.lineno - 1 if anchor else first.end_lineno
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    lines.insert(at, indent + MUTANT + eol)
    mutated = "".join(lines)
    try:
        ast.parse(mutated)
    except SyntaxError:
        return None
    path.write_bytes(mutated.encode("utf-8"))
    return original


def _covers(pred, truth):
    return truth == pred or truth.startswith(pred + "::") or pred.startswith(truth + "::")


def score(pred, truth, all_tests):
    hit = lambda preds: {t for t in truth if any(_covers(p, t) for p in preds)}
    first = next((i + 1 for i, p in enumerate(pred) if any(_covers(p, t) for t in truth)), None)
    run = {t for t in all_tests if any(_covers(p, t) for p in pred)}
    return {
        # top-k can hold at most k failures, so recall@k is out of min(k, failures); a file entry can cover many
        **{f"recall@{k}": min(1.0, len(hit(pred[:k])) / min(k, len(truth))) for k in KS},
        "recall": len(hit(pred)) / len(truth),
        "first_hit": first,
        "predicted": len(pred),
        "precision": sum(any(_covers(p, t) for t in truth) for p in pred) / len(pred) if pred else 0.0,
        "suite_share": len(run) / len(all_tests) if all_tests else 0.0,
    }


def predictors(index, root, sym, test_texts):
    """Ranked test lists from ChangeLens and the two baselines, for the currently mutated tree."""
    name = sym.name.rsplit(".", 1)[-1]
    module = index.by_file[sym.file][0].name
    word = re.compile(rf"\b{re.escape(name)}\b")
    importers = sorted(f for f, syms in index.by_file.items() if is_test(f) and any(
        t == module or t.startswith(module + ".") for t in index.aliases.get(syms[0].name, {}).values()))
    changelens = [t["id"] for t in analyze(index, git_diff(root, "HEAD"), limit=10**6)["tests"]]
    return {
        "changelens": changelens,
        # same ranking collapsed to test files: the like-for-like comparison with the file-level baselines
        "changelens-files": list(dict.fromkeys(t.split("::")[0] for t in changelens)),
        "grep": sorted(f for f, text in test_texts.items() if word.search(text)),
        "importers": importers,
    }


def bench(repo=".", test_cmd="python -m pytest", n=30, seed=0, timeout=900, log=lambda *a: print(*a, file=sys.stderr)):
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("commit or stash tracked changes first: the benchmark diffs each mutant against HEAD")
    index = Index(root)
    all_tests = collect(root, test_cmd, timeout)
    baseline = failing(root, test_cmd, timeout)
    if baseline is None or not all_tests:
        raise ValueError(f"could not run the test suite with {test_cmd!r} in {root}")
    log(f"{len(all_tests)} tests, {len(baseline)} already failing (ignored)")
    test_texts = {f: (root / f).read_text(encoding="utf-8", errors="replace") for f in index.by_file if is_test(f)}
    pool = [s for s in index.symbols.values() if s.kind == "function" and not is_test(s.file)]
    random.Random(seed).shuffle(pool)

    records, tried = [], 0
    for sym in pool:
        if len(records) >= n or tried >= 4 * n:
            break
        original = mutate(root, sym)
        if original is None:
            continue
        tried += 1
        try:
            truth = failing(root, test_cmd, timeout)
            preds = predictors(Index(root), root, sym, test_texts) if truth else None
        finally:
            (root / sym.file).write_bytes(original)
        truth = (truth or set()) - baseline
        if not truth:  # no test exercises this function: nothing to recall
            log(f"  -  {sym.name}: no test fails")
            continue
        rec = {"symbol": sym.name, "file": sym.file, "failed": sorted(truth),
               "scores": {m: score(p, truth, all_tests) for m, p in preds.items()}}
        records.append(rec)
        log(f"{len(records):>3}  {sym.name}: {len(truth)} failed; "
            + ", ".join(f"{m} {s['recall']:.0%}" for m, s in rec["scores"].items()))
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise RuntimeError("working tree not clean after benchmark: check `git diff`")
    return {"repo": str(root), "test_cmd": test_cmd if isinstance(test_cmd, str) else " ".join(test_cmd),
            "seed": seed, "tests": len(all_tests), "mutants_tried": tried, "records": records,
            "summary": summarize(records)}


def _status(root, a, b):
    """git diff --name-status a b -> [(status letter, old path, new path)]."""
    out = []
    for line in git(root, "diff", "--name-status", "--no-renames", a, b).splitlines():
        status, path = line.split("\t", 1)
        out.append((status[0], path))
    return out


def cochange(repo=".", n=50, scan=400, max_files=20, log=lambda *a: print(*a, file=sys.stderr)):
    """PR co-change benchmark (plan §8): replay recent commits that change both source and tests. Rebuild the
    pre-commit state plus only the source changes, and ask which existing test files the change affects;
    ground truth is the test files the commit actually modified (brand-new test files can't be predicted)."""
    import shutil
    import tempfile

    root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    commits = git(root, "log", "--first-parent", "--format=%H %P", f"-n{scan}").splitlines()
    wt = Path(tempfile.mkdtemp(prefix="changelens-cochange-"))
    git(root, "worktree", "add", "--detach", "-f", str(wt), "HEAD")
    records, tried = [], 0
    try:
        for line in commits:
            if len(records) >= n:
                break
            commit, *parents = line.split()
            if not parents:
                continue
            changes = _status(root, parents[0], commit)
            py = [(s, p) for s, p in changes if p.endswith(".py")]
            source = [(s, p) for s, p in py if not is_test(p)]
            truth = {p for s, p in py if s == "M" and is_test(p) and not p.endswith("conftest.py")}
            if not source or not truth or len(source) > max_files:
                continue
            tried += 1
            git(wt, "checkout", "-q", "-f", "--detach", parents[0])
            git(wt, "clean", "-q", "-fd")
            for s, p in source:  # pre-commit tree + this commit's source changes only
                if s == "D":
                    (wt / p).unlink(missing_ok=True)
                else:
                    git(wt, "checkout", commit, "--", p)
            diff = git(wt, "diff", "HEAD", "--", *[p for _, p in source])
            if not diff.strip():
                continue
            index = Index(wt)
            test_files = {f for f in index.by_file if is_test(f) and not f.endswith("conftest.py")}
            result = analyze(index, diff, limit=10**6)
            changelens = list(dict.fromkeys(t["id"].split("::")[0] for t in result["tests"]))
            names = {s.rsplit(".", 1)[-1] for s in result["changed_symbols"]
                     if index.symbols[s].kind in ("function", "class")}
            mods = {index.by_file[p][0].name for _, p in source if p in index.by_file}
            texts = {f: (wt / f).read_text(encoding="utf-8", errors="replace") for f in test_files}
            words_re = [re.compile(rf"\b{re.escape(x)}\b") for x in names]
            preds = {
                "changelens-files": changelens,
                "grep": sorted(f for f in test_files if any(w.search(texts[f]) for w in words_re)),
                "importers": sorted(f for f in test_files if any(
                    t == m or t.startswith(m + ".") for t in index.aliases.get(index.by_file[f][0].name, {}).values()
                    for m in mods)),
            }
            truth &= test_files
            if not truth:
                continue
            records.append({"commit": commit[:10], "subject": git(root, "log", "-1", "--format=%s", commit).strip(),
                            "source": [p for _, p in source], "modified_tests": sorted(truth),
                            "scores": {m: score(p, truth, test_files) for m, p in preds.items()}})
            log(f"{len(records):>3}  {commit[:10]} {records[-1]['subject'][:60]}: "
                + ", ".join(f"{m} {s['recall']:.0%}" for m, s in records[-1]["scores"].items()))
    finally:
        git(root, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
    return {"kind": "cochange", "repo": str(root), "seed": None, "tests": None, "mutants_tried": tried,
            "records": records, "summary": summarize(records)}


def summarize(records):
    out = {}
    for m in (records[0]["scores"] if records else ()):
        rows = [r["scores"][m] for r in records]
        mean = lambda key: sum(r[key] for r in rows) / len(rows)
        out[m] = {**{key: round(mean(key), 3) for key in [f"recall@{k}" for k in KS] + ["recall", "precision", "suite_share", "predicted"]},
                  "any_hit": round(sum(r["recall"] > 0 for r in rows) / len(rows), 3)}
    return out


def report(result):
    s = result["summary"]
    head = (f"{len(result['records'])} commits that change source and existing tests ({result['mutants_tried']} tried); "
            "scored on test files" if result.get("kind") == "cochange" else
            f"{len(result['records'])} covered mutants ({result['mutants_tried']} tried), {result['tests']} tests, "
            f"seed {result['seed']}")
    lines = [f"**{result['repo']}**: {head}", "",
             "| Method | Recall@5 | Recall@10 | Recall | Any hit | Precision | Suite run | Predictions |",
             "|---|---|---|---|---|---|---|---|"]
    for m, v in s.items():
        lines.append(f"| {m} | {v['recall@5']:.0%} | {v['recall@10']:.0%} | {v['recall']:.0%} | {v['any_hit']:.0%} "
                     f"| {v['precision']:.0%} | {v['suite_share']:.0%} | {v['predicted']:.1f} |")
    return "\n".join(lines)
