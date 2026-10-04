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


def test_parse_diff():
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -3,3 +3,3 @@ def f():
 a
-b
+c
 d
@@ -10,2 +10,1 @@
 e
-f
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x
"""
    changed, deleted = parse_diff(diff)
    assert changed == {"x.py": {4, 10}} and deleted == ["gone.py"]
