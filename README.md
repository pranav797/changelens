# ChangeLens

**Know what a code change will break before you run the tests.** ChangeLens reads a Python repo, works out which functions a diff touches, and follows the real dependency graph to tell you which files are affected, which tests to run (most likely failures first), and how risky the change is, with the reason for every answer. It works as a CLI, a local web UI, an MCP server for AI coding agents, and a GitHub PR bot.

## The problem

Every change has a blast radius, and it's rarely obvious.

- **Developers guess.** You either run the whole suite (slow), run the tests next to the file you edited (misses the far-away breakage), or grep for the function name, which in our benchmark finds only 15–38% of the tests that actually fail.
- **AI coding agents guess harder.** An agent only sees the handful of files in its context window. When it edits `round_money`, nothing tells it that checkout, pricing and the cart tests all depend on it, or that renaming an import just broke every module that imports from that package.
- **Reviewers can't see it either.** A PR diff shows what changed, not what depends on it.

## Why ChangeLens is useful

- **It finds what actually breaks.** On four open-source repos (click, jinja, marshmallow, rich), ChangeLens found **96–100% of the tests that really failed** when a function broke, against 15–38% for grep. On real commits, the top 10 test files it predicts contain 78–87% of the tests developers actually touched.
- **It explains itself.** Every affected file and test comes with the chain that reached it (`round_money → Cart.subtotal → Cart → test_total`), so you can trust it or overrule it.
- **It catches certain breaks.** If a diff removes or renames something that other code still uses, ChangeLens reports a certain `NameError`/`ImportError` and which modules fail, before anything runs.
- **It gives agents ground truth.** Served over MCP, it lets Claude (or any MCP client) ask "what does this change affect?" instead of guessing from a partial view of the repo.
- **It's local and honest.** Static analysis plus the standard library, nothing sent anywhere. Every design choice was measured against a benchmark, including the ideas that didn't work (embeddings, a learned ranker): see [docs/benchmark.md](docs/benchmark.md).

## Quick start

```bash
uv tool install git+https://github.com/pranav797/changelens   # puts `changelens` on your PATH
cd your-python-repo
changelens analyze            # what does my uncommitted change affect?
changelens ui                 # the same, as an interactive graph in your browser
```

Or work from a clone: `git clone https://github.com/pranav797/changelens && cd changelens && uv sync`, then prefix commands with `uv run`.

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
4. **Coverage (optional).** If you import one per-test coverage run, tests that actually executed the changed code rank first and are marked `covered`. See below.

Measured on real repos in [docs/benchmark.md](docs/benchmark.md).

## Coverage (optional)

Static analysis finds nearly every test that fails, but it can't tell which of hundreds of candidates *run* the changed code. One coverage run can:

```bash
uv run changelens coverage --run "python -m pytest"   # needs pytest-cov in that environment
uv run changelens coverage --file .coverage           # or import one collected with --cov-context=test
uv run changelens coverage --clear
```

Coverage is mapped to symbols, not lines, so it stays useful as the code changes; re-import it now and then. On Python 3.12+, `--run` sets `COVERAGE_CORE=ctrace`, because the default `sys.monitoring` core can't record per-test contexts.

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
      - uses: pranav797/changelens@master
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

- Static analysis plus a text match can't see everything. Duck-typed calls (`gateway.charge()` on an untyped parameter) are matched by method name and listed as affected files (`"channel": "duck"`), but they don't feed test prediction (benchmarked: no recall gain). Dynamic imports aren't followed.
- Non-Python files are listed as "not traced".
- Recall drops for chains longer than the depth limit (`--depth`).
