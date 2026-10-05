# ChangeLens

Predict what a code change could break. ChangeLens parses a Python repo into a symbol-level reference graph, maps a diff onto the symbols it touches, and walks the reverse graph to find affected files and the tests to run. Every result comes with the dependency chain that reached it, plus an explainable risk score. See [the project plan](ChangeLens_Project_Plan.md).

## Install

```bash
uv sync
```

## CLI

Each subcommand calls the matching MCP tool.

```bash
uv run changelens analyze                              # working tree vs HEAD, Markdown report
uv run changelens analyze --base origin/main...HEAD --json
uv run changelens tests                                # tests for the current change, one id per line
uv run changelens tests --symbol Calc.total            # tests for one symbol
uv run changelens files --symbol pkg.core.add          # affected non-test files
uv run changelens risk                                 # risk table for the current change
uv run changelens deps pkg.core.add                    # depends_on / dependents as JSON
uv run changelens search "proxy auth header"
```

Common flags: `--repo PATH`, `--depth N` (default 5), `--limit N`, `--json`. Change commands take `--base REF` or `--diff-file F` (`-` for stdin).

## Web UI

```bash
uv run changelens ui --repo path/to/repo
```

This opens http://127.0.0.1:8765 with the impact graph (the change at the centre, one ring per dependency step), the impact, tests and risk panels, and a history of past analyses. It can analyze a change against any base, or a single symbol. Use `--port` and `--no-open` to change the port or skip opening a browser.

## MCP server

```bash
claude mcp add changelens -- uv --directory /path/to/changelens run changelens mcp
```

Tools: `analyze_change`, `find_affected_files`, `find_related_tests`, `get_dependency_chain`, `search_codebase`, `explain_risk`. Inputs and consistency rules are in [the plan, §7.1](ChangeLens_Project_Plan.md#71-mcp-tools).

## Known limits (v1)

Risk includes a **breaks** signal: code that still uses a name the diff removed or renamed, or imports from a deleted or now-failing module, is a certain `NameError`/`ImportError` and makes the change high risk on its own.

Static analysis only: duck-typed calls on untyped objects, dynamic imports, and pytest fixtures injected by name aren't traced. Non-Python files are listed as "not traced". The index is rebuilt in memory on every call.
