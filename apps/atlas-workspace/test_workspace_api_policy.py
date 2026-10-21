import ast
from pathlib import Path
import re
from urllib.parse import parse_qs

import pytest


@pytest.fixture(scope="module")
def policy():
    path = Path(__file__).with_name("run_workspace.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"UUID", "PATH"}
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef):
            if node.name == "conversation_request_error":
                selected.append(node)
    assert len(selected) == 3
    namespace = {"re": re, "parse_qs": parse_qs}
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["conversation_request_error"]


CID = "20000000-0000-0000-0000-000000000001"
PID = "30000000-0000-0000-0000-000000000001"
BASE = f"/conversations/{CID}"
LIST = BASE + "/memory-proposals"
REVIEW = LIST + f"/{PID}/review"


@pytest.mark.parametrize("method,path,query", [
    ("GET", "/conversations", ""),
    ("GET", "/conversations", "limit=50&offset=0"),
    ("POST", "/conversations", ""),
    ("GET", BASE, ""),
    ("GET", BASE + "/messages", "limit=100&offset=0"),
    ("POST", BASE + "/messages", ""),
    ("POST", BASE + "/turns", ""),
    ("GET", LIST, ""),
    ("GET", LIST, "status=proposed"),
    ("GET", LIST, "status=accepted&limit=10&offset=0"),
    ("GET", LIST, "status=rejected"),
    ("POST", REVIEW, ""),
])
def test_allowed_requests(policy, method, path, query):
    assert policy(method, path, query) is None


@pytest.mark.parametrize("method,path,query,code", [
    ("GET", BASE + "/turns", "", 405),
    ("GET", REVIEW, "", 405),
    ("POST", LIST, "", 405),
    ("POST", BASE, "", 405),
    ("POST", REVIEW, "status=accepted", 405),
    ("POST", BASE + "/turns", "limit=1", 405),
    ("DELETE", LIST, "", 405),
    ("GET", LIST, "status=unknown", 400),
    ("GET", LIST, "status=", 400),
    ("GET", LIST, "status=accepted&status=rejected", 400),
    ("GET", LIST, "limit=1&limit=2", 400),
    ("GET", LIST, "offset=-1", 400),
    ("GET", LIST, "limit=abc", 400),
    ("GET", LIST, "limit=", 400),
    ("GET", LIST, "limit=%D9%A1", 400),
    ("GET", BASE, "status=accepted", 400),
    ("GET", LIST, "owner=someone-else", 400),
    ("GET", LIST + "/extra", "", 404),
    ("POST", LIST + "/not-a-uuid/review", "", 404),
    ("GET", "/admin", "", 404),
    ("GET", "/conversations/not-a-uuid/memory-proposals", "", 404),
])
def test_rejected_requests(policy, method, path, query, code):
    error = policy(method, path, query)
    assert error is not None
    assert error[0] == code
