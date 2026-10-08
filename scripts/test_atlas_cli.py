import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location(
    "atlas_cli", Path(__file__).with_name("atlas_cli.py")
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

JOB = "33333333-3333-4333-8333-333333333333"
GATE = "44444444-4444-4444-8444-444444444444"


class FakeClient:
    def __init__(self, replies=None):
        self.replies = list(replies or [{"status": "ok"}])
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.replies.pop(0)


class CLITests(unittest.TestCase):
    def test_submit_is_gated_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "working.txt"
            content = " café\nworking copy "
            path.write_text(content, encoding="utf-8")
            client = FakeClient()
            args = cli.parser().parse_args(["submit", str(path)])
            cli.dispatch(args, client)
            payload = client.calls[0][2]
            self.assertTrue(payload["approval_required"])
            self.assertEqual(payload["request_payload"]["content"], content)
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_linked_submission_preserves_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "working.txt"
            raw = b"first line\r\nsecond line\r\n"
            path.write_bytes(raw)
            client = FakeClient()
            args = cli.parser().parse_args([
                "submit", str(path), "--evidence-id", GATE
            ])
            cli.dispatch(args, client)
            payload = client.calls[0][2]
            self.assertEqual(payload["evidence_id"], GATE)
            self.assertEqual(
                payload["request_payload"]["content"].encode("utf-8"), raw
            )
            self.assertEqual(path.read_bytes(), raw)

    def test_explicit_ungated_and_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "working.txt"
            path.write_text("example", encoding="utf-8")
            client = FakeClient()
            args = cli.parser().parse_args([
                "submit", str(path), "--ungated", "--correlation-id", JOB
            ])
            cli.dispatch(args, client)
            self.assertFalse(client.calls[0][2]["approval_required"])
            self.assertEqual(client.calls[0][2]["correlation_id"], JOB)

    def test_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "working.txt"
            for content in (" \n", "x" * 20001):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(cli.CLIError):
                    cli.read_content(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(cli.CLIError):
                cli.read_content(path)
            with self.assertRaises(cli.CLIError):
                cli.read_content(Path(directory) / "missing")

    def test_read_and_execute_routes(self):
        for command, method, suffix in (
            ("show", "GET", ""),
            ("execute", "POST", "/execute"),
            ("result", "GET", "/workflow-result"),
        ):
            with self.subTest(command=command):
                client = FakeClient()
                cli.dispatch(
                    cli.parser().parse_args([command, JOB]), client
                )
                self.assertEqual(
                    client.calls, [(method, f"/jobs/{JOB}{suffix}", None)]
                )

    def test_decisions_resolve_linked_gate(self):
        for command in ("approve", "reject"):
            with self.subTest(command=command):
                client = FakeClient([{"approval_gate_id": GATE}, {"status": command}])
                args = cli.parser().parse_args([
                    command, JOB, "--decided-by", " operator ",
                    "--reason", " Reviewed ",
                ])
                cli.dispatch(args, client)
                self.assertEqual(client.calls[0], ("GET", f"/jobs/{JOB}", None))
                self.assertEqual(client.calls[1], (
                    "POST", f"/approval-gates/{GATE}/{command}",
                    {"decided_by": "operator", "decision_reason": "Reviewed"},
                ))

    def test_missing_or_invalid_gate(self):
        for gate in (None, "not-a-uuid"):
            client = FakeClient([{"approval_gate_id": gate}])
            args = cli.parser().parse_args([
                "approve", JOB, "--decided-by", "operator"
            ])
            with self.assertRaises(cli.CLIError):
                cli.dispatch(args, client)
            self.assertEqual(len(client.calls), 1)

    def test_missing_token_and_unsafe_urls(self):
        with self.assertRaises(cli.CLIError):
            cli.APIClient("http://127.0.0.1:8000", "")
        for url in (
            "http://example.com", "https://user:secret@example.com",
            "https://example.com/path", "https://example.com?token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(cli.CLIError):
                cli.APIClient(url, "token")

    def test_http_request(self):
        response = io.BytesIO(b'{"status":"queued"}')
        with patch.object(cli, "urlopen", return_value=response) as opening:
            result = cli.APIClient("http://127.0.0.1:8000", "test-token").request(
                "POST", "/jobs", {"approval_required": True}
            )
        request = opening.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(result, {"status": "queued"})

    def test_network_and_http_errors_are_sanitized(self):
        errors = [
            URLError("private-detail"),
            HTTPError("http://private", 403, "private-detail", {}, None),
            HTTPError("http://private", 409, "private-detail", {}, None),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(cli, "urlopen", side_effect=error):
                    with self.assertRaises(cli.CLIError) as raised:
                        cli.APIClient("http://localhost:8000", "secret").request(
                            "GET", "/jobs"
                        )
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))

    def test_invalid_json_and_shape(self):
        for body in (b"not-json", b"[]"):
            with patch.object(cli, "urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(cli.CLIError):
                    cli.APIClient("http://localhost:8000", "token").request(
                        "GET", "/jobs"
                    )

    def test_main_failure_exit(self):
        with patch.dict(cli.os.environ, {"ATLAS_TOKEN": ""}, clear=True):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["show", JOB]), 1)

    def test_main_success_json(self):
        with patch.dict(cli.os.environ, {"ATLAS_TOKEN": "token"}, clear=True):
            with patch.object(cli.APIClient, "request", return_value={"status": "queued"}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["show", JOB]), 0)
        self.assertEqual(cli.json.loads(output.getvalue()), {"status": "queued"})


if __name__ == "__main__":
    unittest.main()
