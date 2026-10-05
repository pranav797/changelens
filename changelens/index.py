"""Parse a Python repo into symbols and a reference graph using the stdlib ast."""
import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


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


def _own_names(node, skip):
    """Names/dotted names used directly in node, not inside nested symbols."""
    out, stack = set(), list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if n in skip:
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update(a.asname or a.name for a in n.names if a.name != "*")
            continue
        if isinstance(n, (ast.Name, ast.Attribute)) and (d := _dotted(n)):
            out.add(d)
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


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


class Index:
    # ponytail: full in-memory rebuild per call; persist + re-index incrementally when a repo is too slow
    def __init__(self, repo="."):
        self.root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        self.symbols: dict[str, Symbol] = {}
        self.refs: dict[str, set[str]] = {}
        self.dependents: dict[str, set[str]] = {}
        self.by_file: dict[str, list[Symbol]] = {}
        self.uses: dict[str, set[str]] = {}  # symbol -> bare names it uses (first part of each dotted name)
        self._nodes: dict[str, ast.AST] = {}
        files = git(self.root, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py").splitlines()
        parsed = []
        for f in files:
            try:
                tree = ast.parse((self.root / f).read_bytes(), f)
            except (SyntaxError, ValueError, OSError):
                continue
            parsed.append((f, module_name(f), tree))
            self._collect(f, parsed[-1][1], tree)
        self.aliases = {mod: self._aliases(f, mod, tree) for f, mod, tree in parsed}
        for f, mod, tree in parsed:
            self._link(f, mod)
        for src, targets in self.refs.items():
            for t in targets:
                self.dependents.setdefault(t, set()).add(src)
        del self._nodes

    def _add(self, sym, node):
        self.symbols[sym.name] = sym
        self.by_file.setdefault(sym.file, []).append(sym)
        self._nodes[sym.name] = node

    def _collect(self, f, mod, tree):
        end = max((n.end_lineno for n in tree.body), default=1)
        self._add(Symbol(mod, f, 1, end, 0, "module", ast.get_docstring(tree) or ""), tree)

        def visit(parent, body):
            for node in body:
                if isinstance(node, DEFS):
                    name = f"{parent}.{node.name}"
                    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    self._add(Symbol(name, f, start, node.end_lineno,
                                     max(node.lineno, node.body[0].lineno - 1), kind,
                                     ast.get_docstring(node) or ""), node)
                    if kind == "class":
                        visit(name, node.body)
        visit(mod, tree.body)
        # module-level names (constants, imports, defs inside if/try) get a symbol spanning their statement,
        # so editing that statement reaches the functions that use the name
        for stmt in tree.body:
            if not isinstance(stmt, DEFS):
                for name, is_import in _bound(stmt).items():
                    if f"{mod}.{name}" not in self.symbols:
                        self._add(Symbol(f"{mod}.{name}", f, stmt.lineno, stmt.end_lineno, 0,
                                         "import" if is_import else "variable"), stmt)

    def _aliases(self, f, mod, tree):
        """Local name -> dotted target, from every import in the file."""
        pkg = mod.split(".") if f.endswith("__init__.py") else mod.split(".")[:-1]
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        aliases[a.asname] = a.name
                    else:
                        aliases[a.name.split(".")[0]] = a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(pkg[:len(pkg) - (node.level - 1)] + ([node.module] if node.module else []))
                for a in node.names:
                    if a.name != "*":
                        aliases[a.asname or a.name] = f"{base}.{a.name}"
        return aliases

    def _resolve_parts(self, parts, min_len, hops=5):
        for i in range(len(parts), min_len - 1, -1):
            if (cand := ".".join(parts[:i])) in self.symbols:
                break
        else:
            return None
        # re-exports: `pkg.name` / an import binding, where pkg does `from .api import name`: follow to the real
        # symbol. hops bounds cycles; if the target is outside the repo, the binding itself is the answer.
        kind = self.symbols[cand].kind
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

    def _link(self, f, mod):
        mod_parts = mod.split(".")
        aliases = self.aliases[mod]

        def resolve(dotted, cls):
            parts = dotted.split(".")
            if parts[0] in ("self", "cls") and cls:
                # self.method -> the method; plain instance state (self.items) is not a dependency on the class
                return {self._resolve_parts(cls.split(".") + parts[1:], len(cls.split(".")) + 1)}
            if parts[0] in aliases:  # the real target, plus the import line that binds it here
                return {self._resolve_parts(aliases[parts[0]].split(".") + parts[1:], 1),
                        f"{mod}.{parts[0]}" if f"{mod}.{parts[0]}" in self.symbols else None}
            return {self._resolve_parts(mod_parts + parts, len(mod_parts) + 1)}

        syms = self.by_file[f]
        skip = {self._nodes[s.name] for s in syms if s.kind != "module"}
        for s in syms:
            parent = s.name.rsplit(".", 1)[0]
            cls = s.name if s.kind == "class" else (
                parent if parent in self.symbols and self.symbols[parent].kind == "class" else None)
            node = self._nodes[s.name]
            names = _own_names(node, skip)
            local = set()  # names a function binds itself (params, assignments) are not uses of module names
            if s.kind == "function":
                local = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                local |= {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
            self.uses[s.name] = {d.split(".")[0] for d in names} - local
            refs = set().union(*(resolve(d, cls) for d in names)) - {None}
            if s.kind == "class":  # class -> its members, so users of the class see member changes
                refs |= {c.name for c in syms if c.name.rsplit(".", 1)[0] == s.name}
            refs.discard(s.name)
            self.refs[s.name] = refs

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
