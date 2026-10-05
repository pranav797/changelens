"""Diff -> changed symbols -> reverse-graph impact -> explainable risk."""
import ast
import re
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath

from .index import Index, _bound, git, is_test, module_name, words

# ponytail: hand-set weights; tune against the benchmark (plan §8)
W_FAN_IN, FAN_IN_CAP = 35, 20
W_UNTESTED = 25
W_INTERFACE = 25
W_VOLATILITY = 15
W_BREAKS = 60  # a certain break: high risk on its own
DEFAULT_DEPTH = 5  # benchmarked: 4 -> 5 lifts recall ~3-4 points for ~7% more of the suite (plan §8)
W_NAME_AFFINITY = 2  # test ranking: one shared word is worth two dependency steps
TEXT_DEPTH = 3  # text-channel tests rank like a 3-step dependency (benchmarked: plan §8)
TEXT_MAX_MATCHES = 30  # a name in more test sources than this is too generic to mean anything
TEXT_PREFIXES = ("do_", "get_", "visit_")  # registry/dispatch naming: do_max is the `max` filter
HISTORY_COMMITS = 500
FIX_RE = re.compile(r"\b(fix|fixes|fixed|bug|revert|hotfix|regression)\b", re.I)
CONFIG_RE = re.compile(r"(\.(toml|ya?ml|ini|cfg|json|env)$|(^|/)(setup\.py|Dockerfile|requirements[^/]*\.txt)$)")
SCHEMA_RE = re.compile(r"(^|/)(migrations?|schemas?|models?)(/|\.py$)")


def git_diff(repo=".", base="HEAD") -> str:
    if base.startswith("-"):  # would be parsed as a git option (e.g. --output=FILE writes a file)
        raise ValueError(f"base must be a git revision, not an option: {base!r}")
    return git(repo, "diff", base)


def _bound_by_line(text):
    """Names one top-level source line binds (`def f(a):` -> f, `from x import y` -> y); empty if unparsable."""
    for src in (text, text + " pass"):  # " pass" completes a bare `def f():` / `class C:` header
        try:
            return {n for stmt in ast.parse(src).body for n in _bound(stmt)}
        except SyntaxError:
            continue
    return set()


def parse_diff(text):
    """Unified diff -> ({path: changed new-side lines}, [deleted paths], {path: names on removed top-level lines})."""
    changed, deleted, removed, path, new, header, pending = {}, [], {}, None, 0, False, None
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
            if line[1:2].strip():  # unindented: a module-level statement lost the names it bound
                removed.setdefault(path, set()).update(_bound_by_line(line[1:]))
        elif line.startswith(" "):
            new += 1
    return changed, deleted, removed


def _member_edge(index, src, dst):
    return src.startswith(dst + ".") and index.symbols[dst].kind == "class"  # dst is the class containing src


def reach(index, seeds, max_depth=DEFAULT_DEPTH, edges=None):
    """0-1 BFS over reverse edges (or `edges`). Member->class edges are free. Returns {symbol: (depth, via)}."""
    edges = index.dependents if edges is None else edges
    free = edges is index.dependents  # member->class is free only toward dependents
    dist = {s: (0, None) for s in seeds}
    q = deque(seeds)
    while q:
        s = q.popleft()
        for d in sorted(edges.get(s, ())):
            w = 0 if free and _member_edge(index, s, d) else 1
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
    # conftest.py is pytest setup, not something to run: its breakage still shows up under risk breaks
    return (is_test(sym.file) and not sym.file.endswith("conftest.py")
            and (sym.kind == "module" or last.startswith(("test", "Test"))))


def _test_id(index, name):
    sym = index.symbols[name]
    if sym.kind == "module":
        return sym.file
    module = index.by_file[sym.file][0].name
    return sym.file + "::" + name[len(module) + 1:].replace(".", "::")


def text_names(index, seeds):
    """Names a test's source would use to exercise these symbols, even through templates, string-keyed
    registries or getattr dispatch: each seed's own name and its direct users' names (do_max -> max)."""
    names = set()
    for s in seeds:
        for n in [s, *index.dependents.get(s, ())]:
            last = n.rsplit(".", 1)[-1]
            if last.startswith("__") or n in index.fixtures:  # fixture links are already exact in the graph
                continue
            last = last.lstrip("_")
            for prefix in TEXT_PREFIXES:
                last = last.removeprefix(prefix)
            if len(last) >= 4:
                names.add(last)
    return names


def text_matches(index, seeds):
    """Channel B: test functions whose source names a seed (or a direct user of one), in code or strings."""
    found = {}
    for name in text_names(index, seeds):
        hits = [t for t, tokens in index.test_tokens.items() if name in tokens]
        if len(hits) <= TEXT_MAX_MATCHES:
            for t in hits:
                found.setdefault(t, name)
    return found


def tests_for(index, dist):
    tests = {}
    for name, (depth, _) in dist.items():
        if _is_test_item(index, name):
            tid = _test_id(index, name)
            if tid not in tests or depth < tests[tid]["depth"]:
                tests[tid] = {"id": tid, "depth": depth, "why": _chain(dist, name), "channel": "graph"}
    seeds = [s for s, (depth, _) in dist.items() if depth == 0]
    covered = {}
    if index.coverage:  # tests that executed the changed code in the last coverage run: rank first
        ids = {_test_id(index, n): n for n in index.symbols if _is_test_item(index, n)}
        for s in seeds:
            for tid in index.coverage.get(s, ()):
                covered.setdefault(tid, s)
        for tid, seed in covered.items():
            if tid not in tests and tid in ids:
                tests[tid] = {"id": tid, "depth": 1, "why": [seed, ids[tid]], "channel": "coverage"}
    for name, matched in text_matches(index, seeds).items():
        tid = _test_id(index, name)
        if tid not in tests:
            root = next((s for s in seeds if matched in (s, *index.dependents.get(s, ()))
                         or matched in text_names(index, [s])), seeds[0])
            tests[tid] = {"id": tid, "depth": TEXT_DEPTH, "why": [root, name], "channel": "text", "matched": matched}
    if index.coverage:
        for t in tests.values():
            t["covered"] = t["id"] in covered
    # a file or Test class is redundant once one of its own tests is listed
    parents = {tid.rsplit("::", i)[0] for tid in tests for i in range(1, tid.count("::") + 1)}
    return sorted((t for t in tests.values() if t["id"] not in parents), key=_rank)


def name_affinity(symbol, test_id):
    """Words a changed symbol shares with a test's id (file, class, function), ignoring the package name:
    click.core.Command.format_help and tests/test_commands.py::test_help_format share {command, format, help}."""
    stop = {"test", "tests", symbol.split(".")[0]}
    return len((set(words(symbol.split(".", 1)[-1])) - stop) & (set(words(test_id.replace(".py", ""))) - stop))


def _rank(test):
    # benchmarked on click (plan §8): depth alone ranks the tests that actually fail poorly once a hub class
    # pulls in half the suite; a test named after what changed is the strongest tie-breaker (recall@10 44% -> 57%)
    return (not test.get("covered", False), test["depth"] - W_NAME_AFFINITY * name_affinity(test["why"][0], test["id"]),
            test["id"])


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


@dataclass
class Change:
    lines: dict        # file -> changed new-side line numbers
    deleted: list      # deleted files
    symbols: list      # symbols the diff edits
    interface: set     # changed symbols whose public signature changed
    breaks: dict       # symbol -> {reason: cause}: a certain NameError/ImportError, and the name/module behind it

    @property
    def seeds(self):
        """Where impact starts: what was edited, plus what will fail because of it."""
        return sorted(set(self.symbols) | set(self.breaks))


def changes(index, diff):
    lines, deleted, removed = parse_diff(diff)
    changed, interface = set(), set()
    for f, nums in lines.items():
        for line in nums:
            for sym in index.symbols_at(f, line):
                changed.add(sym.name)
                if sym.kind in ("class", "function") and sym.public and line <= sym.sig_end and not is_test(f):
                    interface.add(sym.name)
    # a symbol the diff added whole is new: no existing caller can depend on its signature
    interface = {s for s in interface
                 if not set(range(index.symbols[s].start, index.symbols[s].end + 1)) <= lines[index.symbols[s].file]}

    breaks = {}  # symbol -> {reason: cause}, in discovery order so root causes come first

    def broke(syms, reason, cause):
        for s in sorted(syms & index.symbols.keys()):
            breaks.setdefault(s, {}).setdefault(reason, cause)

    gone = {}  # dotted name the diff removed at module level (and didn't re-bind) -> root cause
    for f, names in removed.items():
        still = {s.name.rsplit(".", 1)[-1] for s in index.by_file.get(f, ())}
        mod = index.by_file[f][0].name if f in index.by_file else module_name(f)
        for name in sorted(names - still):
            gone[f"{mod}.{name}"] = f"{mod}.{name} no longer exists"
            broke(index.users_of(f, {name}), f"{name} is no longer defined in {f}", f"{mod}.{name}")  # same file
    gone_mods = {module_name(f): f"{f} was deleted" for f in deleted if f.endswith(".py")}  # module -> root cause
    while True:  # importers of gone names / failing modules; repeat to follow re-exports and import chains
        before = len(gone) + len(gone_mods)
        for mod, aliases in index.aliases.items():
            if mod not in index.symbols:
                continue
            for local, target in sorted(aliases.items(), key=lambda kv: kv[1] not in gone):  # direct causes first
                if target in gone:
                    root = reason = gone[target]
                    cause = target
                else:  # importing from a failing module (not counting the package this module lives in)
                    m = next((m for m in gone_mods if (target == m or target.startswith(m + "."))
                              and not (mod == m or mod.startswith(m + "."))), None)
                    if m is None:
                        continue
                    root, reason, cause = gone_mods[m], f"imports from {m}: {gone_mods[m]}", m
                binding = f"{mod}.{local}"
                gone.setdefault(binding, root)  # a re-export of a gone name is gone too
                if binding in index.symbols and index.symbols[binding].kind == "import":
                    if mod not in gone_mods:  # a failing top-level import fails the whole module
                        gone_mods[mod] = root
                        broke({mod}, reason, cause)
                else:  # import inside a function: only its users fail
                    broke(index.users_of(index.symbols[mod].file, {local}), reason, cause)
        if len(gone) + len(gone_mods) == before:
            break
    # a module that fails to import is one entry; the symbols inside it are implied
    module_of = {s: index.by_file[index.symbols[s].file][0].name for s in breaks}
    breaks = {s: r for s, r in breaks.items() if s in gone_mods or module_of[s] not in gone_mods}
    return Change(lines, deleted, sorted(changed), interface, breaks)


def affected_files(index, dist, exclude=()):
    """Non-test files reached at depth >= 1, one entry per file at its shallowest depth; plus files that call a
    changed method by name on an untyped object (channel "duck": benchmarked, it only adds noise to test
    prediction, so it informs this list and nothing else)."""
    files = {}
    for name, (depth, _) in dist.items():
        f = index.symbols[name].file
        if depth and not is_test(f) and f not in exclude and (f not in files or depth < files[f]["depth"]):
            files[f] = {"file": f, "depth": depth, "why": _chain(dist, name), "channel": "graph"}
    for seed in sorted(s for s, (depth, _) in dist.items() if depth == 0):
        for user in sorted(index.duck_users(seed)):
            f = index.symbols[user].file
            if not is_test(f) and f not in exclude and f not in files:
                files[f] = {"file": f, "depth": 1, "why": [seed, user], "channel": "duck"}
    return sorted(files.values(), key=lambda x: (x["depth"], x["file"]))


def risk(index, change, max_depth=DEFAULT_DEPTH):
    changed, interface = change.symbols, change.interface
    # unused module-level names (e.g. a freshly added import) can't break anything, and code that will fail
    # outright is covered by the breaks signal, so neither counts here
    code = [s for s in changed if not is_test(index.symbols[s].file) and s not in change.breaks
            and (index.symbols[s].kind not in ("import", "variable") or index.dependents.get(s))]
    fan_in = {d for s in code for d in index.dependents.get(s, ())
              if d not in changed and not _member_edge(index, s, d) and not is_test(index.symbols[d].file)}
    untested = [s for s in code if not tests_for(index, reach(index, [s], max_depth))]
    touched, fixes = volatility(index.root, set(change.lines) | set(change.deleted))
    breaks = [f"{s} ({next(iter(r))})" for s, r in change.breaks.items()]
    signals = [
        ("fan-in", len(fan_in), W_FAN_IN * min(len(fan_in), FAN_IN_CAP) / FAN_IN_CAP,
         f"{len(fan_in)} non-test symbols directly reference the change"),
        ("untested", len(untested), W_UNTESTED * len(untested) / len(code) if code else 0,
         f"{len(untested)}/{len(code)} changed symbols are reached by no test" + (f": {', '.join(untested[:5])}" if untested else "")),
        ("interface", len(interface), W_INTERFACE if interface else 0,
         f"public signature changed: {', '.join(sorted(interface))}" if interface else "no public signature changed"),
        ("volatility", fixes, W_VOLATILITY * fixes / touched if touched else 0,
         f"{fixes}/{touched} recent commits touching these files were fixes"),
        ("breaks", len(breaks), W_BREAKS if breaks else 0,
         f"{len(breaks)} symbols will fail: " + "; ".join(breaks[:3]) + ("; ..." if len(breaks) > 3 else "") if breaks
         else "no removed name is still referenced"),
    ]
    score = min(100, round(sum(p for _, _, p, _ in signals)))

    all_files = list(change.lines) + change.deleted
    types = []
    if all_files and all(is_test(f) for f in all_files):
        types.append("test-only")
    if any(CONFIG_RE.search(f) for f in all_files):
        types.append("config")
    if any(SCHEMA_RE.search(f) for f in all_files):
        types.append("schema")
    if breaks:
        types.append("breaking")
    if interface:
        types.append("api")
    if set(code) - interface:
        types.append("logic")
    return {
        "score": score,
        "level": "high" if score >= 60 else "medium" if score >= 30 else "low",
        "change_types": types,
        "signals": [{"name": n, "value": v, "points": round(p, 1), "why": w} for n, v, p, w in signals],
        "breaks": [{"symbol": s, "file": index.symbols[s].file, "reasons": list(r),
                    "causes": list(dict.fromkeys(r.values()))} for s, r in change.breaks.items()],
    }


def analyze(index: Index, diff: str, max_depth=DEFAULT_DEPTH, limit=50):
    change = changes(index, diff)
    dist = reach(index, change.seeds, max_depth)
    return {
        "changed_files": sorted(change.lines),
        "changed_symbols": change.symbols,
        "affected_files": affected_files(index, dist, change.lines)[:limit],
        "tests": tests_for(index, dist)[:limit],
        "risk": risk(index, change, max_depth),
        # ponytail: non-Python files are reported, not traced (deleted .py files are traced via their importers)
        "untraced": sorted(f for f in [*change.lines, *change.deleted] if PurePosixPath(f).suffix != ".py"),
    }


def risk_markdown(risk):
    out = [f"## ChangeLens: {risk['level'].upper()} risk ({risk['score']}/100)",
           f"Change types: {', '.join(risk['change_types']) or 'none'}", "",
           "| Signal | Points | Why |", "|---|---|---|"]
    out += [f"| {s['name']} | {s['points']} | {s['why']} |" for s in risk["signals"]]
    if risk["breaks"]:
        out += ["", "### Will break"]
        out += [f"- `{b['symbol']}` ({b['file']}): {'; '.join(b['reasons'])}" for b in risk["breaks"][:10]]
        if len(risk["breaks"]) > 10:
            out.append(f"- ...and {len(risk['breaks']) - 10} more")
    return "\n".join(out)


def _test_line(t):
    ran = " **ran this code**" if t.get("covered") else ""
    if t["channel"] == "text":
        return f"- `{t['id']}`{ran} (text match: test source names `{t['matched']}`)"
    if t["channel"] == "coverage":
        return f"- `{t['id']}`{ran} (found by coverage, not the dependency graph)"
    return f"- `{t['id']}`{ran} (depth {t['depth']}) via {' → '.join(t['why'])}"


def to_markdown(r):
    out = [risk_markdown(r["risk"]), "", f"### Tests to run ({len(r['tests'])})"]
    out += [_test_line(t) for t in r["tests"]] or ["- none found"]
    out += ["", f"### Affected files ({len(r['affected_files'])})"]
    out += [f"- `{a['file']}` (depth {a['depth']}) via {' → '.join(a['why'])}" if a["channel"] == "graph" else
            f"- `{a['file']}` (likely: `{a['why'][1]}` calls `.{a['why'][0].rsplit('.', 1)[-1]}()` on an untyped object)"
            for a in r["affected_files"]] or ["- none found"]
    if r["untraced"]:
        out += ["", "### Not traced", *[f"- `{f}`" for f in r["untraced"]]]
    return "\n".join(out)
