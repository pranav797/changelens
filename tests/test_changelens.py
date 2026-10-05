import subprocess

import pytest

from changelens import server
from changelens.analyze import parse_diff

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\n\nclass Calc:\n    def total(self, xs):\n        return sum(add(x, 0) for x in xs)\n',
    "pkg/api.py": "from .core import Calc\n\n\ndef run():\n    return Calc().total([1])\n",
    "tests/test_api.py": "from pkg.api import run\n\n\ndef test_run():\n    assert run() == 1\n",
    "tests/test_other.py": "def test_nothing():\n    pass\n\n\ndef run():\n    pass\n",
}


def sh(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    for path, text in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    sh(tmp_path, "init")
    sh(tmp_path, "add", ".")
    sh(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_change_tools_agree(repo):
    # body change: reaches test_run through add -> Calc.total -> Calc -> run -> test_run
    core = repo / "pkg/core.py"
    core.write_text(FILES["pkg/core.py"].replace("return a + b", "return b + a"))
    r = server.analyze_change(str(repo))
    assert r["changed_symbols"] == ["pkg.core.add"]
    tests = {t["id"]: t for t in r["tests"]}
    assert "tests/test_api.py::test_run" in tests and "tests/test_other.py" not in str(tests)
    assert tests["tests/test_api.py::test_run"]["why"][0] == "pkg.core.add"
    assert [a["file"] for a in r["affected_files"]] == ["pkg/api.py"]
    assert r["risk"]["change_types"] == ["logic"]
    assert next(s for s in r["risk"]["signals"] if s["name"] == "untested")["value"] == 0

    # the per-question tools return exactly the matching slice of analyze_change
    assert server.find_affected_files(str(repo))["affected_files"] == r["affected_files"]
    assert server.find_related_tests(str(repo))["tests"] == r["tests"]
    assert server.explain_risk(str(repo)) == {"changed_symbols": r["changed_symbols"], "risk": r["risk"]}

    # signature change on a public function -> api
    core.write_text(FILES["pkg/core.py"].replace("def add(a, b):", "def add(a, b, c=0):"))
    risk = server.explain_risk(str(repo))["risk"]
    assert "api" in risk["change_types"]
    assert next(s for s in risk["signals"] if s["name"] == "interface")["points"] == 25


def test_symbol_tools(repo):
    assert [t["id"] for t in server.find_related_tests(str(repo), symbol="Calc.total")["tests"]] == ["tests/test_api.py::test_run"]
    assert server.find_affected_files(str(repo), symbol="pkg.core.add")["affected_files"][0]["file"] == "pkg/api.py"

    chain = server.get_dependency_chain(str(repo), "pkg.api.run")
    assert [d["symbol"] for d in chain["depends_on"]][:1] == ["pkg.core.Calc"]
    assert "tests.test_api.test_run" in [d["symbol"] for d in chain["dependents"]]

    assert server.search_codebase(str(repo), "add numbers")["results"][0]["symbol"] == "pkg.core.add"

    with pytest.raises(ValueError, match="2 symbols match 'run'"):  # pkg.api.run and tests.test_other.run
        server.get_dependency_chain(str(repo), "run")
    with pytest.raises(ValueError, match="not both"):
        server.find_related_tests(str(repo), diff="x", symbol="Calc")


def test_module_level_changes(repo):
    (repo / "pkg/money.py").write_text(
        "from decimal import ROUND_HALF_UP, Decimal\n\nRATE = 2\n\n\n"
        "def r(x):\n    return Decimal(x).quantize(1, ROUND_HALF_UP) * RATE\n")
    (repo / "tests/test_money.py").write_text("from pkg.money import r\n\n\ndef test_r():\n    assert r(1) == 2\n")
    sh(repo, "add", ".")
    sh(repo, "commit", "-m", "money")
    money = (repo / "pkg/money.py").read_text()

    # renaming an import leaves r() with a NameError: r must be flagged, and its test found
    (repo / "pkg/money.py").write_text(money.replace("import ROUND_HALF_UP", "import ROUND_HALF_EVEN"))
    r = server.analyze_change(str(repo))
    assert r["changed_symbols"] == ["pkg.money.Decimal", "pkg.money.ROUND_HALF_EVEN"]  # edited; r only breaks
    assert [t["id"] for t in r["tests"]] == ["tests/test_money.py::test_r"]
    assert r["risk"]["breaks"] == [{"symbol": "pkg.money.r", "file": "pkg/money.py",
                                    "reasons": ["ROUND_HALF_UP is no longer defined in pkg/money.py"],
                                    "causes": ["pkg.money.ROUND_HALF_UP"]}]
    assert r["risk"]["level"] == "high" and "breaking" in r["risk"]["change_types"]

    # a module constant reaches its users, with the constant at the head of the "why" chain; nothing breaks
    (repo / "pkg/money.py").write_text(money.replace("RATE = 2", "RATE = 3"))
    r = server.analyze_change(str(repo))
    assert r["changed_symbols"] == ["pkg.money.RATE"]
    assert r["tests"][0]["why"] == ["pkg.money.RATE", "pkg.money.r", "tests.test_money.test_r"]
    assert r["risk"]["breaks"] == []
    assert server.get_dependency_chain(str(repo), "RATE")["dependents"][0]["symbol"] == "pkg.money.r"

    # renaming a class another module imports: the importer's import line fails, so does the whole module,
    # so does whatever imports from that module
    (repo / "pkg/money.py").write_text(money)
    core = repo / "pkg/core.py"
    core.write_text(FILES["pkg/core.py"].replace("class Calc:", "class Calculator:"))
    breaks = {b["symbol"]: (b["reasons"], b["causes"]) for b in server.explain_risk(str(repo))["risk"]["breaks"]}
    assert breaks == {"pkg.api": (["pkg.core.Calc no longer exists"], ["pkg.core.Calc"]),
                      "tests.test_api": (["imports from pkg.api: pkg.core.Calc no longer exists"], ["pkg.api"])}

    # deleting a module breaks whoever imports from it
    core.write_text(FILES["pkg/core.py"])
    (repo / "pkg/money.py").unlink()
    r = server.analyze_change(str(repo))
    assert r["risk"]["breaks"] == [{"symbol": "tests.test_money", "file": "tests/test_money.py",
                                    "reasons": ["imports from pkg.money: pkg/money.py was deleted"],
                                    "causes": ["pkg.money"]}]
    assert r["tests"] == [{"id": "tests/test_money.py", "depth": 0, "why": ["tests.test_money"]}]


def test_parse_diff():
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -3,3 +3,3 @@ def f():
 a
-B = 1
+B = 2
 d
@@ -10,2 +10,1 @@
 e
-def f(a):
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x
"""
    changed, deleted, removed = parse_diff(diff)
    assert changed == {"x.py": {4, 10}} and deleted == ["gone.py"] and removed == {"x.py": {"B", "f"}}


def test_ui_api(repo):
    import json
    import threading
    import urllib.error
    import urllib.request

    from changelens.ui import make_server

    httpd = make_server(str(repo), 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def post(body, headers=None):
        headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(base + "/api/analyze", json.dumps(body).encode(), headers)
        return json.load(urllib.request.urlopen(req))

    try:
        assert b"<title>ChangeLens</title>" in urllib.request.urlopen(base + "/").read()
        (repo / "pkg/core.py").write_text(FILES["pkg/core.py"].replace("return a + b", "return b + a"))
        assert post({"base": "HEAD"})["result"]["changed_symbols"] == ["pkg.core.add"]
        sym = post({"symbol": "Calc.total"})["result"]
        assert [t["id"] for t in sym["tests"]] == ["tests/test_api.py::test_run"] and sym["risk"] is None
        history = json.load(urllib.request.urlopen(base + "/api/history"))["history"]
        assert [h["query"] for h in history] == [{"symbol": "Calc.total"}, {"base": "HEAD"}]  # newest first

        # option injection, non-JSON (cross-site form) posts, and DNS-rebinding hosts are all refused
        for body, headers, code in [({"base": "--output=pwned"}, None, 400),
                                    ({"base": "HEAD"}, {"Content-Type": "text/plain"}, 415),
                                    ({"base": "HEAD"}, {"Host": "evil.example"}, 403)]:
            with pytest.raises(urllib.error.HTTPError) as err:
                post(body, headers)
            assert err.value.code == code
        assert not (repo / "pwned").exists()
    finally:
        httpd.shutdown()


def test_bench(repo):
    import sys

    from changelens.bench import bench, mutate, report
    from changelens.index import Index

    # mutation inserts a raise after the docstring and restores byte-for-byte
    core = repo / "pkg/core.py"
    before = core.read_bytes()
    original = mutate(repo, Index(repo).symbols["pkg.core.add"])
    assert original == before and 'raise RuntimeError("changelens mutant")\n    return a + b' in core.read_text()
    core.write_bytes(original)

    result = bench(repo, [sys.executable, "-m", "pytest"], n=3, log=lambda *a: None)
    assert {r["symbol"] for r in result["records"]} == {"pkg.core.add", "pkg.core.Calc.total", "pkg.api.run"}
    assert all(r["failed"] == ["tests/test_api.py::test_run"] for r in result["records"])
    assert result["summary"]["changelens"]["recall"] == 1.0
    assert "| changelens |" in report(result)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_reexport_shadows_submodule(repo):
    # pkg/__init__ does `from .checkout import checkout`: `from pkg import checkout` is the function, not the module
    (repo / "pkg/checkout.py").write_text("def checkout():\n    return 1\n")
    (repo / "pkg/__init__.py").write_text("from .checkout import checkout\n")
    (repo / "tests/test_co.py").write_text("from pkg import checkout\n\n\ndef test_co():\n    assert checkout() == 1\n")
    tests = server.find_related_tests(str(repo), symbol="pkg.checkout.checkout")["tests"]
    assert [t["id"] for t in tests] == ["tests/test_co.py::test_co"]
