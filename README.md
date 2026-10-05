# ChangeLens

Predict what a code change could break. ChangeLens parses a Python repo into a symbol-level reference graph, maps a diff onto the symbols it touches, and walks the reverse graph to find affected files and the tests to run. Every result comes with the dependency chain that reached it, plus an explainable risk score. See [the project plan](ChangeLens_Project_Plan.md) and [the benchmark](docs/benchmark.md).

## Install

```bash
uv sync
```

## CLI

Each subcommand calls the matching MCP tool.

```bash
uv run changelens analyze                              # working tree vs HEAD, Markdown report
uv run changelens analyze --base origin/main...HEAD --json
uv run changelens tests                                # tests for the current change, most likely first
uv run changelens tests --symbol Calc.total            # tests for one symbol
uv run changelens files --symbol pkg.core.add          # affected non-test files
uv run changelens risk --fail-on breaks                # risk table; exit 1 if the change will break code
uv run changelens deps pkg.core.add                    # depends_on / dependents as JSON
uv run changelens search "proxy auth header"
```

Common flags: `--repo PATH`, `--depth N` (default 5), `--limit N`, `--json`. Change commands take `--base REF` or `--diff-file F` (`-` for stdin). `analyze` and `risk` take `--fail-on breaks|high|medium` for CI.

## How tests are found

1. **Dependency graph.** Calls, imports, re-exports, inheritance and pytest fixtures (parameters, `usefixtures`, autouse, conftest scoping) are traced from the changed symbols up to 5 steps.
2. **Text channel.** Adds tests whose source names the changed code, or a direct user of it, even inside strings. This catches templates, string-keyed registries and `getattr` dispatch that no graph sees. These are marked `"channel": "text"`.
3. **Ranking.** Tests with fewer dependency steps come first, and tests named after what changed get a boost.

Measured on real repos in [docs/benchmark.md](docs/benchmark.md).

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

## GitHub PR bot

```yaml
# .github/workflows/changelens.yml
on: pull_request
permissions: { contents: read, pull-requests: write }
jobs:
  impact:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }      # base branch + history for the volatility signal
      - uses: <owner>/changelens@<ref>
        with:
          fail-on: breaks             # or high / medium; "" never fails
```

The bot writes the report to the job summary, posts one PR comment and updates that same comment on later pushes, then applies the `fail-on` gate. Pull requests from forks get a read-only token, so they only get the job summary.

## Benchmark

```bash
uv run changelens bench --repo path/to/repo --test-cmd ".venv/Scripts/python -m pytest" -n 40 --out results.json
```

This makes one function at a time raise, runs the real test suite, and scores how much of what actually failed ChangeLens predicted, compared with grep and direct-importer baselines. The repo must have no uncommitted changes to tracked files; every mutated file is restored byte for byte.

## Performance

The index is cached per file in `.git/changelens/index.json` (JSON, keyed by content). Django (2.9k files, 64k symbols): 13s cold, 2.6s warm.

## Risk

Each signal contributes visible points: fan-in, untested, interface change, volatility, and **breaks**. A break is code that still uses a name the diff removed or renamed, or that imports from a deleted or now-failing module. It is a certain `NameError`/`ImportError` and makes the change high risk on its own.

## Known limits

- Static analysis plus a text match can't see everything. Duck-typed calls on untyped objects (`gateway.charge()`) don't make the caller an *affected file*, and dynamic imports aren't followed.
- Non-Python files are listed as "not traced".
- Recall drops for chains longer than the depth limit (`--depth`).
