import argparse
import json
import sys

from . import server
from .analyze import risk_markdown, to_markdown

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
            s.add_argument("--depth", type=int, default=4)
        if name != "risk":
            s.add_argument("--limit", type=int, default=10 if name == "search" else 50)
    sub.add_parser("mcp", help="run the MCP server over stdio")
    u = sub.add_parser("ui", help="open the interactive web UI for a repo")
    u.add_argument("--repo", default=".")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-open", action="store_true", help="don't open a browser")
    b = sub.add_parser("bench", help="mutation benchmark: recall of predicted tests vs grep/importer baselines")
    b.add_argument("--repo", default=".")
    b.add_argument("--test-cmd", default="python -m pytest", help="how to run the repo's tests (pytest)")
    b.add_argument("-n", type=int, default=30, help="covered mutants to score")
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--out", help="also write full per-mutant results as JSON here")
    args = p.parse_args(argv)

    if args.cmd == "bench":
        from .bench import bench, report
        try:
            result = bench(args.repo, args.test_cmd, args.n, args.seed)
        except ValueError as e:
            sys.exit(f"changelens: {e}")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
        print(report(result))
        return
    if args.cmd == "mcp":
        server.mcp.run()
        return
    if args.cmd == "ui":
        from .ui import serve
        serve(args.repo, args.port, not args.no_open)
        return

    tool = COMMANDS[args.cmd][0]
    kwargs = {k: v for k, v in vars(args).items() if k not in ("cmd", "json", "diff_file")}
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


if __name__ == "__main__":
    main()
