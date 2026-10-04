"""Diff -> changed symbols -> reverse-graph impact -> explainable risk."""
import re
import subprocess
from collections import deque
from pathlib import PurePosixPath

from .index import Index, git, is_test

# ponytail: hand-set weights; tune against the benchmark (plan §8)
W_FAN_IN, FAN_IN_CAP = 35, 20
W_UNTESTED = 25
W_INTERFACE = 25
W_VOLATILITY = 15
HISTORY_COMMITS = 500
FIX_RE = re.compile(r"\b(fix|fixes|fixed|bug|revert|hotfix|regression)\b", re.I)
CONFIG_RE = re.compile(r"(\.(toml|ya?ml|ini|cfg|json|env)$|(^|/)(setup\.py|Dockerfile|requirements[^/]*\.txt)$)")
SCHEMA_RE = re.compile(r"(^|/)(migrations?|schemas?|models?)(/|\.py$)")


def git_diff(repo=".", base="HEAD") -> str:
    return git(repo, "diff", base)


def parse_diff(text):
    """Unified diff -> ({path: changed new-side line numbers}, [deleted paths])."""
    changed, deleted, path, new, header, pending = {}, [], None, 0, False, None
    for line in text.splitlines() + [""]:
        if pending and not line.startswith(("-", "+")):
            changed[path].add(pending)  # pure deletion: sits between new-1 and new
            pending = None
        if line.startswith("diff --git"):
            header, path = True, None
        elif header and line.startswith("--- "):
            old = line[4:].split("\t")[0]
        elif header and line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            if p == "/dev/null":
                deleted.append(old[2:] if old.startswith("a/") else old)
            else:
                path = p[2:] if p.startswith("b/") else p
                changed.setdefault(path, set())
        elif line.startswith("@@"):
            header = False
            new = int(re.match(r"@@ -\S+ \+(\d+)", line).group(1))
        elif header or path is None:
            continue
        elif line.startswith("+"):
            changed[path].add(new)
            new += 1
            pending = None  # replacement: the added line already marks the spot
        elif line.startswith("-"):
            pending = max(new - 1, 1)
        elif line.startswith(" "):
            new += 1
    return changed, deleted


def _member_edge(src, dst):
    return src.startswith(dst + ".")  # dst is the class containing src


def reach(index, seeds, max_depth=4, edges=None):
    """0-1 BFS over reverse edges (or `edges`). Member->class edges are free. Returns {symbol: (depth, via)}."""
    edges = index.dependents if edges is None else edges
    dist = {s: (0, None) for s in seeds}
    q = deque(seeds)
    while q:
        s = q.popleft()
        for d in sorted(edges.get(s, ())):
            w = 0 if _member_edge(s, d) else 1
            nd = dist[s][0] + w
            if nd <= max_depth and (d not in dist or nd < dist[d][0]):
                dist[d] = (nd, s)
                q.appendleft(d) if w == 0 else q.append(d)
    return dist


def _chain(dist, s):
    out = [s]
    while (s := dist[s][1]) is not None:
        out.append(s)
    return out[::-1]


def _is_test_item(index, name):
    sym = index.symbols[name]
    last = name.rsplit(".", 1)[-1]
    return is_test(sym.file) and (sym.kind == "module" or last.startswith(("test", "Test")))


def _test_id(index, name):
    sym = index.symbols[name]
    if sym.kind == "module":
        return sym.file
    module = index.by_file[sym.file][0].name
    return sym.file + "::" + name[len(module) + 1:].replace(".", "::")


def tests_for(index, dist):
    tests = {}
    for name, (depth, _) in dist.items():
        if _is_test_item(index, name):
            tid = _test_id(index, name)
            if tid not in tests or depth < tests[tid]["depth"]:
                tests[tid] = {"id": tid, "depth": depth, "why": _chain(dist, name)}
    # a file or Test class is redundant once one of its own tests is listed
    parents = {tid.rsplit("::", i)[0] for tid in tests for i in range(1, tid.count("::") + 1)}
    return sorted((t for t in tests.values() if t["id"] not in parents), key=lambda t: (t["depth"], t["id"]))


def volatility(root, files):
    try:
        log = git(root, "log", f"-n{HISTORY_COMMITS}", "--format=%x00%s", "--name-only")
    except subprocess.CalledProcessError:
        return 0, 0
    touched = fixes = 0
    for chunk in log.split("\x00")[1:]:
        subject, *names = chunk.split("\n")
        if files & set(names):
            touched += 1
            fixes += bool(FIX_RE.search(subject))
    return touched, fixes


def changes(index, diff):
    """Diff -> (changed_lines, deleted_files, changed symbols, symbols whose public signature changed)."""
    changed_lines, deleted = parse_diff(diff)
    changed, interface = set(), set()
    for f, lines in changed_lines.items():
        for line in lines:
            if sym := index.symbol_at(f, line):
                changed.add(sym.name)
                if sym.kind != "module" and sym.public and line <= sym.sig_end:
                    interface.add(sym.name)
    return changed_lines, deleted, sorted(changed), interface


def affected_files(index, dist, exclude=()):
    """Non-test files reached at depth >= 1, one entry per file at its shallowest depth."""
    files = {}
    for name, (depth, _) in dist.items():
        f = index.symbols[name].file
        if depth and not is_test(f) and f not in exclude and (f not in files or depth < files[f]["depth"]):
            files[f] = {"file": f, "depth": depth, "why": _chain(dist, name)}
    return sorted(files.values(), key=lambda x: (x["depth"], x["file"]))


def risk(index, changed_lines, deleted, changed, interface, max_depth=4):
    code = [s for s in changed if not is_test(index.symbols[s].file)]
    fan_in = {d for s in code for d in index.dependents.get(s, ())
              if d not in changed and not _member_edge(s, d) and not is_test(index.symbols[d].file)}
    untested = [s for s in code if not tests_for(index, reach(index, [s], max_depth))]
    touched, fixes = volatility(index.root, set(changed_lines) | set(deleted))
    signals = [
        ("fan-in", len(fan_in), W_FAN_IN * min(len(fan_in), FAN_IN_CAP) / FAN_IN_CAP,
         f"{len(fan_in)} non-test symbols directly reference the change"),
        ("untested", len(untested), W_UNTESTED * len(untested) / len(code) if code else 0,
         f"{len(untested)}/{len(code)} changed symbols are reached by no test" + (f": {', '.join(untested[:5])}" if untested else "")),
        ("interface", len(interface), W_INTERFACE if interface else 0,
         f"public signature changed: {', '.join(sorted(interface))}" if interface else "no public signature changed"),
        ("volatility", fixes, W_VOLATILITY * fixes / touched if touched else 0,
         f"{fixes}/{touched} recent commits touching these files were fixes"),
    ]
    score = round(sum(p for _, _, p, _ in signals))

    all_files = list(changed_lines) + deleted
    types = []
    if all_files and all(is_test(f) for f in all_files):
        types.append("test-only")
    if any(CONFIG_RE.search(f) for f in all_files):
        types.append("config")
    if any(SCHEMA_RE.search(f) for f in all_files):
        types.append("schema")
    if interface:
        types.append("api")
    if set(code) - interface:
        types.append("logic")
    return {
        "score": score,
        "level": "high" if score >= 60 else "medium" if score >= 30 else "low",
        "change_types": types,
        "signals": [{"name": n, "value": v, "points": round(p, 1), "why": w} for n, v, p, w in signals],
    }


def analyze(index: Index, diff: str, max_depth=4, limit=50):
    changed_lines, deleted, changed, interface = changes(index, diff)
    dist = reach(index, changed, max_depth)
    return {
        "changed_files": sorted(changed_lines),
        "changed_symbols": changed,
        "affected_files": affected_files(index, dist, changed_lines)[:limit],
        "tests": tests_for(index, dist)[:limit],
        "risk": risk(index, changed_lines, deleted, changed, interface, max_depth),
        # ponytail: deleted files and non-Python files are reported, not traced
        "untraced": sorted(deleted + [f for f in changed_lines if PurePosixPath(f).suffix != ".py"]),
    }


def risk_markdown(risk):
    out = [f"## ChangeLens: {risk['level'].upper()} risk ({risk['score']}/100)",
           f"Change types: {', '.join(risk['change_types']) or 'none'}", "",
           "| Signal | Points | Why |", "|---|---|---|"]
    return "\n".join(out + [f"| {s['name']} | {s['points']} | {s['why']} |" for s in risk["signals"]])


def to_markdown(r):
    out = [risk_markdown(r["risk"]), "", f"### Tests to run ({len(r['tests'])})"]
    out += [f"- `{t['id']}` (depth {t['depth']}) via {' → '.join(t['why'])}" for t in r["tests"]] or ["- none found"]
    out += ["", f"### Affected files ({len(r['affected_files'])})"]
    out += [f"- `{a['file']}` (depth {a['depth']}) via {' → '.join(a['why'])}" for a in r["affected_files"]] or ["- none found"]
    if r["untraced"]:
        out += ["", "### Not traced", *[f"- `{f}`" for f in r["untraced"]]]
    return "\n".join(out)
