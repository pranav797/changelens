"""Local web UI (`changelens ui`): one static page plus a small JSON API over the same functions as the MCP tools."""
import json
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from . import server
from .analyze import DEFAULT_DEPTH, affected_files, analyze, git_diff, tests_for
from .index import Index, git, state_dir

HISTORY_SHOWN = 50
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def history_file(repo):
    """Per-repo history inside .git, so it is never committed."""
    return state_dir(repo) / "history.jsonl"


def read_history(repo):
    path = history_file(repo)
    if not path.exists():
        return []
    # ponytail: append-only file, read whole; rotate if it ever gets big
    lines = path.read_text(encoding="utf-8").splitlines()[-HISTORY_SHOWN:]
    return [json.loads(line) for line in reversed(lines)]


def run(repo, q):
    """One analysis: a change (`base`) or one `symbol`, in analyze_change's shape plus `symbol_files` (for the
    graph's group-by-file view)."""
    depth, limit = int(q.get("depth", DEFAULT_DEPTH)), int(q.get("limit", 200))
    if q.get("symbol"):
        index, seeds, dist, exclude = server._start(repo, "HEAD", "", q["symbol"], depth)
        result = {"changed_files": [], "changed_symbols": seeds, "affected_files": affected_files(index, dist, exclude)[:limit],
                  "tests": tests_for(index, dist)[:limit], "risk": None, "untraced": []}
    else:
        index = Index(repo)
        result = analyze(index, git_diff(index.root, q.get("base") or "HEAD"), depth, limit)
    named = {*result["changed_symbols"], *(s for e in result["affected_files"] + result["tests"] for s in e["why"])}
    for b in (result["risk"] or {}).get("breaks", ()):
        named |= {b["symbol"], *b["causes"]}
    result["symbol_files"] = {s: index.symbols[s].file for s in named if s in index.symbols}
    return result


class Handler(BaseHTTPRequestHandler):
    repo = "."  # set per server in serve()

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        # DNS rebinding: a hostile page that resolves to 127.0.0.1 still sends its own Host header
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
            self._send(403, {"error": "forbidden host"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        if self.path == "/":
            self._send(200, files("changelens").joinpath("ui.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/history":
            self._send(200, {"repo": self.repo, "history": read_history(self.repo)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # JSON-only POST: browsers must preflight a cross-site JSON request, which this server never approves
        if not self._guard():
            return
        if self.path != "/api/analyze" or self.headers.get("Content-Type") != "application/json":
            return self._send(404 if self.path != "/api/analyze" else 415, {"error": "POST JSON to /api/analyze"})
        try:
            q = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "query": q, "result": run(self.repo, q)}
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        except subprocess.CalledProcessError as e:
            return self._send(400, {"error": (e.stderr or str(e)).strip()})
        path = history_file(self.repo)
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self._send(200, record)

    def log_message(self, *args):
        pass


def make_server(repo=".", port=8765):
    root = git(repo, "rev-parse", "--show-toplevel").strip()
    return ThreadingHTTPServer(("127.0.0.1", port), type("RepoHandler", (Handler,), {"repo": root}))


def serve(repo=".", port=8765, open_browser=True):
    httpd = make_server(repo, port)
    url = f"http://127.0.0.1:{httpd.server_port}/"
    print(f"ChangeLens UI for {httpd.RequestHandlerClass.repo}\n  {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
