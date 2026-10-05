"""MCP tools. The CLI calls these same functions, so both interfaces always agree.

Conventions shared by every tool:
- `repo` is any path inside a git repo.
- A *change* is `diff` (unified diff text) if given, else `git diff <base>` (default: working tree vs HEAD;
  use "origin/main...HEAD" for a branch). The change tools return exactly the matching slice of `analyze_change`.
- `symbol` is a qualified name or dotted suffix ("Calc.total"); ambiguous or unknown names raise an error listing candidates.
- Every result entry carries `depth` and `why` (the dependency chain that reached it).
"""
from mcp.server.mcpserver import MCPServer

from .analyze import _chain, affected_files, analyze, changes, git_diff, reach, risk, tests_for
from .index import Index

mcp = MCPServer("changelens")


def _change(repo, base, diff):
    index = Index(repo)
    return index, diff or git_diff(index.root, base)


def _start(repo, base, diff, symbol, max_depth):
    """Reach from one symbol, or from every symbol the change touches -> (index, seeds, dist, files to exclude)."""
    if symbol and diff:
        raise ValueError("pass either `symbol` or a change (`diff`/`base`), not both")
    if symbol:
        index = Index(repo)
        seeds = [index.lookup(symbol)]
        exclude = {index.symbols[seeds[0]].file}
    else:
        index, diff = _change(repo, base, diff)
        change = changes(index, diff)
        seeds, exclude = change.seeds, change.lines
    return index, seeds, reach(index, seeds, max_depth), exclude


@mcp.tool()
def analyze_change(repo: str, base: str = "HEAD", diff: str = "", max_depth: int = 4, limit: int = 50) -> dict:
    """Full impact report for a change: changed symbols, affected files, tests to run, and explainable risk."""
    index, diff = _change(repo, base, diff)
    return analyze(index, diff, max_depth, limit)


@mcp.tool()
def find_affected_files(repo: str, base: str = "HEAD", diff: str = "", symbol: str = "",
                        max_depth: int = 4, limit: int = 50) -> dict:
    """Non-test files that (transitively) depend on a change, or on one `symbol`. The starting files are excluded."""
    index, seeds, dist, exclude = _start(repo, base, diff, symbol, max_depth)
    return {"from_symbols": seeds, "affected_files": affected_files(index, dist, exclude)[:limit]}


@mcp.tool()
def find_related_tests(repo: str, base: str = "HEAD", diff: str = "", symbol: str = "",
                       max_depth: int = 4, limit: int = 50) -> dict:
    """Tests that (transitively) exercise a change, or one `symbol`, as pytest node ids, shallowest first."""
    index, seeds, dist, _ = _start(repo, base, diff, symbol, max_depth)
    return {"from_symbols": seeds, "tests": tests_for(index, dist)[:limit]}


@mcp.tool()
def get_dependency_chain(repo: str, symbol: str, max_depth: int = 4, limit: int = 50) -> dict:
    """Both directions for one symbol: what it depends on, and what depends on it."""
    index = Index(repo)
    name = index.lookup(symbol)

    def walk(edges):
        dist = reach(index, [name], max_depth, edges)
        # import-line nodes are bookkeeping for "this import changed"; the symbols they import are listed instead
        return [{"symbol": s, "file": index.symbols[s].file, "depth": d, "why": _chain(dist, s)}
                for s, (d, _) in sorted(dist.items(), key=lambda kv: (kv[1][0], kv[0]))
                if s != name and index.symbols[s].kind != "import"][:limit]

    return {"symbol": name, "file": index.symbols[name].file,
            "depends_on": walk(index.refs), "dependents": walk(index.dependents)}


@mcp.tool()
def search_codebase(repo: str, query: str, limit: int = 10) -> dict:
    """Find symbols by words in their name, module path, or docstring (e.g. "proxy auth header")."""
    return {"query": query, "results": Index(repo).search(query, limit)}


@mcp.tool()
def explain_risk(repo: str, base: str = "HEAD", diff: str = "", max_depth: int = 4) -> dict:
    """Risk score for a change: points and reason per signal (fan-in, untested, interface, volatility, breaks).

    `breaks` lists code that still uses a name the diff removed: a certain NameError/ImportError.
    """
    index, diff = _change(repo, base, diff)
    change = changes(index, diff)
    return {"changed_symbols": change.symbols, "risk": risk(index, change, max_depth)}
