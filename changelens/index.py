"""Parse a Python repo into symbols and a reference graph using the stdlib ast."""
import ast
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
CACHE_VERSION = 3  # bump whenever extract() output changes
RACY_NS = 2_000_000_000  # like git: a file modified this close to the cache write may have changed unseen


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


def state_dir(root):
    """Per-repo ChangeLens state (index cache, UI history), inside .git so it is never committed."""
    return Path(git(root, "rev-parse", "--absolute-git-dir").strip()) / "changelens"


def is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return (p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"
            or bool({"tests", "test"} & set(p.parts[:-1])))


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class Symbol:
    name: str      # qualified: pkg.mod.Class.method; a module's own symbol is pkg.mod
    file: str      # repo-relative posix path
    start: int
    end: int
    sig_end: int   # last line of decorators + def/class header
    kind: str      # module | class | function | variable | import (the last two: module-level bindings)
    doc: str = ""

    @property
    def public(self) -> bool:
        return not self.name.rsplit(".", 1)[-1].startswith("_")


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _bound(stmt):
    """Names a module-level statement binds -> True if bound by an import."""
    names = {}
    for n in ast.walk(stmt):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.setdefault(n.id, False)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update({(a.asname or a.name).split(".")[0]: True for a in n.names if a.name != "*"})
        elif isinstance(n, DEFS):
            names.setdefault(n.name, False)
    return names


def _import_record(n):
    return [isinstance(n, ast.ImportFrom), getattr(n, "level", 0), getattr(n, "module", None),
            [[a.name, a.asname] for a in n.names]]


def _scan(node, skip):
    """One pass over a symbol's own region (not nested symbols): dotted names used, names assigned,
    parameter names, and import statements."""
    names, stores, args, imports = set(), set(), set(), []
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        imports.append(_import_record(node))
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if n in skip:
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            imports.append(_import_record(n))
            names.update(a.asname or a.name for a in n.names if a.name != "*")
            continue
        if isinstance(n, ast.arg):
            args.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            stores.add(n.id)
        if isinstance(n, (ast.Name, ast.Attribute)) and (d := _dotted(n)):
            names.add(d)
            continue
        stack.extend(ast.iter_child_nodes(n))
    return names, stores, args, imports


def _usefixtures(node):
    return [a.value for d in node.decorator_list if isinstance(d, ast.Call)
            and (_dotted(d.func) or "").endswith("usefixtures")
            for a in d.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def extract(f, source):
    """Everything the index needs from one file, as plain JSON-able data (this is what gets cached)."""
    tree = ast.parse(source, f)
    mod = module_name(f)
    syms, nodes = [], {}

    def add(row, node):
        syms.append(row)
        nodes[row[0]] = node

    add([mod, 1, max((n.end_lineno for n in tree.body), default=1), 0, "module", ast.get_docstring(tree) or ""], tree)

    def visit(parent, body):
        for node in body:
            if isinstance(node, DEFS):
                name = f"{parent}.{node.name}"
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                add([name, min([d.lineno for d in node.decorator_list] + [node.lineno]), node.end_lineno,
                     max(node.lineno, node.body[0].lineno - 1), kind, ast.get_docstring(node) or ""], node)
                if kind == "class":
                    visit(name, node.body)
    visit(mod, tree.body)
    # module-level names (constants, imports, defs inside if/try) get a symbol spanning their statement,
    # so editing that statement reaches the functions that use the name
    for stmt in tree.body:
        if not isinstance(stmt, DEFS):
            for name, is_import in _bound(stmt).items():
                if f"{mod}.{name}" not in nodes:
                    add([f"{mod}.{name}", stmt.lineno, stmt.end_lineno, 0, "import" if is_import else "variable", ""], stmt)

    skip = {node for name, node in nodes.items() if name != mod}
    own, fixtures, params, usefix = {}, {}, {}, {}
    test_file = is_test(f)
    for name, node in nodes.items():
        n, s, a, im = _scan(node, skip)
        own[name] = [sorted(n), sorted(s), sorted(a), im]
        if not test_file or not isinstance(node, DEFS):
            continue
        usefix[name] = _usefixtures(node)
        if isinstance(node, ast.ClassDef):
            continue
        params[name] = [p.arg for p in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                        if p.arg not in ("self", "cls")]
        for d in node.decorator_list:
            call = d if isinstance(d, ast.Call) else None
            if (_dotted(call.func if call else d) or "").rsplit(".", 1)[-1] == "fixture":
                kw = {k.arg: k.value for k in call.keywords} if call else {}
                fixtures[name] = [kw["name"].value if isinstance(kw.get("name"), ast.Constant) else node.name,
                                  isinstance(kw.get("autouse"), ast.Constant) and kw["autouse"].value is True]
    return {"syms": syms, "own": own, "fixtures": fixtures, "params": params, "usefix": usefix}


def _alias_map(f, mod, records):
    """Import records -> {local name: dotted target}, resolving relative imports against the package."""
    pkg = mod.split(".") if f.endswith("__init__.py") else mod.split(".")[:-1]
    aliases = {}
    for is_from, level, module, names in records:
        if not is_from:
            for name, asname in names:
                aliases[asname or name.split(".")[0]] = name if asname else name.split(".")[0]
            continue
        base = module or ""
        if level:
            base = ".".join(pkg[:len(pkg) - (level - 1)] + ([module] if module else []))
        for name, asname in names:
            if name != "*":
                aliases[asname or name] = f"{base}.{name}"
    return aliases


class Index:
    def __init__(self, repo=".", cache=True):
        self.root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        self.symbols: dict[str, Symbol] = {}
        self.refs: dict[str, set[str]] = {}
        self.dependents: dict[str, set[str]] = {}
        self.by_file: dict[str, list[Symbol]] = {}
        self.uses: dict[str, set[str]] = {}  # symbol -> bare names it uses (first part of each dotted name)
        files = git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py").splitlines()
        facts = self._facts(files, cache)
        # definitions first, then module-level bindings only where no definition has that name (e.g. a package's
        # `from . import utils` must not shadow the real pkg.utils module)
        for bindings in (False, True):
            for f, fx in facts.items():
                for row in fx["syms"]:
                    if (row[4] in ("import", "variable")) == bindings and row[0] not in self.symbols:
                        self.symbols[row[0]] = Symbol(row[0], f, *row[1:])
        for f, fx in list(facts.items()):
            kept = [r for r in fx["syms"] if self.symbols[r[0]].file == f]
            if not kept or kept[0] is not fx["syms"][0]:  # another file already defines this module name
                for r in kept:
                    del self.symbols[r[0]]
                del facts[f]
                continue
            mod_own = fx["own"][kept[0][0]]
            for r in fx["syms"]:  # a dropped binding's statement still belongs to this module
                if r not in kept:
                    names, stores, args, imports = fx["own"].pop(r[0])
                    names = {*names, *(asname or name for _, _, _, ns in imports for name, asname in ns if name != "*")}
                    fx["own"][kept[0][0]] = mod_own = [sorted({*mod_own[0], *names}), sorted({*mod_own[1], *stores}),
                                                       sorted({*mod_own[2], *args}), mod_own[3] + imports]
            fx["syms"] = kept
            self.by_file[f] = [self.symbols[r[0]] for r in kept]
        self.aliases = {fx["syms"][0][0]: self._aliases(f, fx) for f, fx in facts.items()}
        for f, fx in facts.items():
            self._link(f, fx)
        self._link_fixtures(facts)
        for src, targets in self.refs.items():
            for t in targets:
                self.dependents.setdefault(t, set()).add(src)

    def _facts(self, files, use_cache):
        """extract() per file, reusing cached results for files whose content hasn't changed."""
        path = state_dir(self.root) / "index.json"
        cached = {}
        if use_cache:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") == CACHE_VERSION:
                    cached, written = data["files"], data["written_ns"]
            except (OSError, ValueError, KeyError):
                cached = {}
        out, entries, dirty = {}, {}, not cached
        for f in files:
            try:
                st = (self.root / f).stat()
            except OSError:
                continue
            hit = cached.get(f)
            if hit and hit[:2] == [st.st_mtime_ns, st.st_size] and st.st_mtime_ns < written - RACY_NS:
                entries[f] = hit  # unchanged and not racy: skip reading it at all
            else:
                try:
                    source = (self.root / f).read_bytes()
                except OSError:
                    continue
                digest = hashlib.blake2b(source, digest_size=16).hexdigest()
                if hit and hit[2] == digest:
                    entries[f] = [st.st_mtime_ns, st.st_size, digest, hit[3]]
                else:
                    try:
                        fx = extract(f, source)
                    except (SyntaxError, ValueError, RecursionError):
                        fx = None
                    entries[f] = [st.st_mtime_ns, st.st_size, digest, fx]
                dirty = True
            if entries[f][3] is not None:
                out[f] = entries[f][3]
        if use_cache and (dirty or len(entries) != len(cached)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"version": CACHE_VERSION, "written_ns": time.time_ns(), "files": entries}),
                               encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass  # a cache we can't write is just a slower next run
        return out

    def _aliases(self, f, fx):
        """Module-level imports: local name -> dotted target (function-level imports are scoped in _link)."""
        records = [r for row in fx["syms"] if row[4] in ("module", "import", "variable") for r in fx["own"][row[0]][3]]
        return _alias_map(f, fx["syms"][0][0], records)

    def _resolve_parts(self, parts, min_len, hops=5):
        for i in range(len(parts), min_len - 1, -1):
            if (cand := ".".join(parts[:i])) in self.symbols:
                break
        else:
            return None
        # re-exports: `pkg.name` / an import binding, where pkg does `from .api import name`: follow to the real
        # symbol. hops bounds cycles; if the target is outside the repo, the binding itself is the answer.
        kind = self.symbols[cand].kind
        if kind == "module" and "." in cand and hops:
            # `from .checkout import checkout` in pkg/__init__ makes pkg.checkout the function, not the submodule
            parent, last = cand.rsplit(".", 1)
            target = self.aliases.get(parent, {}).get(last)
            if target and target != cand and (real := self._resolve_parts(target.split(".") + parts[i:], 1, hops - 1)):
                return real
        if kind == "module" and i < len(parts):
            owner, name, rest = cand, parts[i], parts[i + 1:]
        elif kind == "import":
            (owner, name), rest = cand.rsplit(".", 1), parts[i:]
        else:
            return cand
        target = self.aliases.get(owner, {}).get(name)
        if target and hops and (real := self._resolve_parts(target.split(".") + rest, 1, hops - 1)):
            return real
        return cand

    def _link(self, f, fx):
        mod = fx["syms"][0][0]
        mod_parts = mod.split(".")
        module_aliases = self.aliases[mod]

        def resolve(dotted, cls, aliases):
            parts = dotted.split(".")
            if parts[0] in ("self", "cls") and cls:
                # self.method -> the method; plain instance state (self.items) is not a dependency on the class
                return {self._resolve_parts(cls.split(".") + parts[1:], len(cls.split(".")) + 1)}
            if parts[0] in aliases:  # the real target, plus the import line that binds it here
                return {self._resolve_parts(aliases[parts[0]].split(".") + parts[1:], 1),
                        f"{mod}.{parts[0]}" if f"{mod}.{parts[0]}" in self.symbols else None}
            return {self._resolve_parts(mod_parts + parts, len(mod_parts) + 1)}

        members = {}  # class -> its direct members
        for s in self.by_file[f]:
            members.setdefault(s.name.rsplit(".", 1)[0], []).append(s.name)
        for s in self.by_file[f]:
            parent = s.name.rsplit(".", 1)[0]
            cls = s.name if s.kind == "class" else (
                parent if parent in self.symbols and self.symbols[parent].kind == "class" else None)
            names, stores, args, imports = fx["own"][s.name]
            local = set(stores) | set(args) if s.kind == "function" else set()  # a function's own names
            self.uses[s.name] = {d.split(".")[0] for d in names} - local
            aliases = module_aliases
            if imports and s.kind in ("function", "class"):  # imports inside a def are scoped to it
                aliases = {**module_aliases, **_alias_map(f, mod, imports)}
            refs = set().union(*(resolve(d, cls, aliases) for d in names)) - {None}
            if s.kind == "class":  # class -> its members, so users of the class see member changes
                refs.update(members.get(s.name, ()))
            refs.discard(s.name)
            self.refs[s.name] = refs

    def _link_fixtures(self, facts):
        """pytest injects fixtures by parameter name: link each test/fixture to the fixtures it would receive."""
        fixtures = {}  # fixture name -> [(symbol, autouse)]
        for fx in facts.values():
            for sym, (name, autouse) in fx["fixtures"].items():
                fixtures.setdefault(name, []).append((self.symbols[sym], autouse))

        def scope(fixture, file):
            """How closely a fixture applies to file: same module beats the nearest conftest.py; None if unseen."""
            if fixture.file == file:
                return (2, 0)
            where = PurePosixPath(fixture.file)
            if where.name == "conftest.py":
                d = where.parent.as_posix()
                if d == "." or file.startswith(d + "/"):
                    return (1, len(where.parent.parts))
            return None

        autouse = [fixture for found in fixtures.values() for fixture, auto in found if auto]
        for f, fx in facts.items():
            for name, params in fx["params"].items():
                s = self.symbols[name]
                wanted = params + fx["usefix"].get(name, []) + fx["usefix"].get(name.rsplit(".", 1)[0], [])
                for want in wanted:
                    seen = [(sc, fix) for fix, _ in fixtures.get(want, ()) if fix is not s and (sc := scope(fix, f))]
                    if seen:
                        self.refs[name].add(max(seen, key=lambda x: x[0])[1].name)
                if name.rsplit(".", 1)[-1].startswith("test"):
                    self.refs[name].update(fix.name for fix in autouse if fix is not s and scope(fix, f))

    def symbols_at(self, file, line):
        """Innermost symbol(s) covering a line; several when one statement binds several names."""
        syms = self.by_file.get(file)
        if not syms:
            return []
        hits = [s for s in syms if s.start <= line <= s.end]
        if not hits:
            return [syms[0]]  # syms[0] is the module
        span = min(s.end - s.start for s in hits)
        return [s for s in hits if s.end - s.start == span]

    def users_of(self, file, names):
        """Symbols in file that use any of these bare names."""
        return {s.name for s in self.by_file.get(file, ()) if self.uses.get(s.name, set()) & names}

    def find(self, query):
        if query in self.symbols:
            return [query]
        return sorted(n for n, s in self.symbols.items() if n.endswith("." + query) and s.kind != "import")

    def lookup(self, query):
        """Exactly one symbol for a qualified name or dotted suffix, else ValueError listing candidates."""
        names = self.find(query)
        if len(names) != 1:
            hint = f"; candidates: {', '.join(names[:20])}" if names else "; try search_codebase"
            raise ValueError(f"{len(names)} symbols match {query!r}{hint}")
        return names[0]

    # ponytail: lexical scoring; swap in embeddings (plan §4) behind the same signature if the benchmark says so
    def search(self, query, limit=10):
        q = set(words(query))
        if not q:
            raise ValueError("query has no searchable words")
        hits = []
        for s in self.symbols.values():
            if s.kind == "import":
                continue
            last, path, doc = set(words(s.name.rsplit(".", 1)[-1])), set(words(s.name)), set(words(s.doc))
            score = sum(3 * (w in last) + (w in path) + (w in doc) for w in q) / (5 * len(q))
            if score:
                hits.append((-score, len(s.name), s))
        return [{"symbol": s.name, "file": s.file, "line": s.start, "kind": s.kind, "score": round(-neg, 2),
                 "doc": s.doc.split("\n", 1)[0]} for neg, _, s in sorted(hits, key=lambda h: h[:2])[:limit]]


def words(text):
    """snake_case / CamelCase / prose -> lowercase words: HTTPAdapter.get_auth -> http adapter get auth."""
    found = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", text)
    return [w.lower().removesuffix("s") if len(w) > 3 else w.lower() for w in found]  # crude plural folding
