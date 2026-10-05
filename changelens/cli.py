import argparse
import json
import sqlite3
import sys

from . import server
from .analyze import DEFAULT_DEPTH, risk_markdown, to_markdown

FAIL_ON = ("breaks", "high", "medium")
LEVELS = ("low", "medium", "high")

# subcommand -> (MCP tool, accepts a change, accepts --symbol, positional arg)
COMMANDS = {
    "analyze": (server.analyze_change, True, False, None),
    "files": (server.find_affected_files, True, True, None),
    "tests": (server.find_related_tests, True, True, None),
    "risk": (server.explain_risk, True, False, None),
    "deps": (server.get_dependency_chain, False, False, "symbol"),
    "search": (server.search_codebase, False, False, "query"),
}


def main(argv=None):
    p = argparse.ArgumentParser(prog="changelens", description="Predict what a code change could break.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, (tool, change, symbol, positional) in COMMANDS.items():
        s = sub.add_parser(name, help=tool.__doc__.split("\n")[0])
        s.add_argument("--repo", default=".")
        s.add_argument("--json", action="store_true")
        if positional:
            s.add_argument(positional)
        if change:
            s.add_argument("--base", default="HEAD", help="passed to `git diff`, e.g. origin/main...HEAD")
            s.add_argument("--diff-file", help="read a unified diff from this file ('-' for stdin) instead of git")
        if symbol:
            s.add_argument("--symbol", default="", help="start from this symbol instead of a change")
        if name != "search":
            s.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
        if name != "risk":
            s.add_argument("--limit", type=int, default=10 if name == "search" else 50)
        if name in ("analyze", "risk"):
            s.add_argument("--fail-on", choices=FAIL_ON, help="exit 1 if the change will break code or reaches this risk level")
    sub.add_parser("mcp", help="run the MCP server over stdio")
    u = sub.add_parser("ui", help="open the interactive web UI for a repo")
    u.add_argument("--repo", default=".")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-open", action="store_true", help="don't open a browser")
    c = sub.add_parser("coverage", help="import per-test coverage so tests that ran the changed code rank first")
    c.add_argument("--repo", default=".")
    c.add_argument("--file", default=".coverage", help="a .coverage file collected with --cov-context=test")
    c.add_argument("--run", metavar="TEST_CMD", help='collect it first by running this, e.g. "python -m pytest"')
    c.add_argument("--clear", action="store_true", help="forget imported coverage")
    b = sub.add_parser("bench", help="mutation benchmark: recall of predicted tests vs grep/importer baselines")
    b.add_argument("--repo", default=".")
    b.add_argument("--test-cmd", default="python -m pytest", help="how to run the repo's tests (pytest)")
    b.add_argument("-n", type=int, default=30, help="covered mutants to score")
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--out", help="also write full per-mutant results as JSON here")
    b.add_argument("--cov", action="store_true", help="also score ChangeLens with per-test coverage (needs pytest-cov)")
    b.add_argument("--prs", action="store_true",
                   help="co-change mode: replay recent commits, score predicted test files vs the ones each commit modified")
    args = p.parse_args(argv)

    if args.cmd == "bench":
        from .bench import bench, cochange, report
        try:
            result = (cochange(args.repo, args.n) if args.prs
                      else bench(args.repo, args.test_cmd, args.n, args.seed, use_coverage=args.cov))
        except ValueError as e:
            sys.exit(f"changelens: {e}")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
        print(report(result))
        return
    if args.cmd == "coverage":
        from . import coverage
        from .index import Index
        index = Index(args.repo)
        if args.clear:
            coverage.clear(index.root)
            print("coverage cleared")
            return
        try:
            path = coverage.collect(index.root, args.run) if args.run else index.root / args.file
            data = coverage.import_coverage(index, path)
        except (ValueError, OSError, sqlite3.Error) as e:
            sys.exit(f"changelens: {e}")
        print(f"imported coverage of {data['tests']} tests over {len(data['symbols'])} symbols (at {data['commit'][:10]})")
        return
    if args.cmd == "mcp":
        server.mcp.run()
        return
    if args.cmd == "ui":
        from .ui import serve
        serve(args.repo, args.port, not args.no_open)
        return

    tool = COMMANDS[args.cmd][0]
    kwargs = {k: v for k, v in vars(args).items() if k not in ("cmd", "json", "diff_file", "fail_on")}
    if "depth" in kwargs:
        kwargs["max_depth"] = kwargs.pop("depth")
    if getattr(args, "diff_file", None):
        kwargs["diff"] = sys.stdin.read() if args.diff_file == "-" else open(args.diff_file, encoding="utf-8").read()
    try:
        result = tool(**kwargs)
    except ValueError as e:
        sys.exit(f"changelens: {e}")

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.cmd == "analyze":
        print(to_markdown(result))
    elif args.cmd == "risk":
        print(risk_markdown(result["risk"]))
    elif args.cmd == "tests":
        print("\n".join(t["id"] for t in result["tests"]))  # pipeable: pytest $(changelens tests)
    elif args.cmd == "files":
        print("\n".join(f["file"] for f in result["affected_files"]))
    else:
        print(json.dumps(result, indent=2))
    if getattr(args, "fail_on", None) and fails(result["risk"], args.fail_on):
        sys.exit(1)


def fails(risk, fail_on):
    """CI gate: `breaks` fails only on a certain break; `high`/`medium` fail at or above that risk level."""
    if fail_on == "breaks":
        return bool(risk["breaks"])
    return LEVELS.index(risk["level"]) >= LEVELS.index(fail_on)


if __name__ == "__main__":
    main()
