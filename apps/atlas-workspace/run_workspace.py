
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
import json
import sqlite3
import re
import shutil
import subprocess
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent
PORT = 8766
TOKEN = "dev-requester"

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = build_opener(NoRedirect())

def locate_backend():
    ports = []
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Ports}}"],
            capture_output=True, text=True, timeout=8
        )
        for match in re.finditer(r":(\d+)->", result.stdout):
            port = int(match.group(1))
            if port not in ports:
                ports.append(port)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for port in (8000, 8001, 8081, 8008, 8080):
        if port not in ports:
            ports.append(port)
    for port in ports[:24]:
        base = f"http://127.0.0.1:{port}"
        try:
            with opener.open(base + "/openapi.json", timeout=2) as response:
                schema = json.loads(response.read(4_000_000))
            routes = schema.get("paths", {})
            required = {
                "/conversations",
                "/conversations/{conversation_id}/messages",
                "/conversations/{conversation_id}/turns",
            }
            if not required.issubset(routes):
                continue
            request = Request(
                base + "/conversations?limit=1&offset=0",
                headers={"Authorization":"Bearer " + TOKEN}
            )
            with opener.open(request, timeout=4) as response:
                data = json.loads(response.read(1_000_000))
            if isinstance(data.get("items"), list):
                return base
        except Exception:
            continue
    raise SystemExit(
        "STOP: no reachable Atlas conversation API accepted the existing "
        "development identity. Files installed; no services changed."
    )

BACKEND = locate_backend()
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
PATH = re.compile(
    rf"/conversations(?:/{UUID}(?:/messages|/turns|/memory-proposals"
    rf"(?:/{UUID}/review)?)?)?"
)


def conversation_request_error(method, path, query_string):
    """Return a bounded proxy-policy error, or None for an allowed request."""

    # atlas-voice-stream-bridge-v1
    voice_path = re.fullmatch(
        rf"/conversations/{UUID}/voice-turns(?:/{UUID}/cancel)?",
        path,
    )
    if voice_path:
        if method != "POST" or query_string:
            return 405, "Voice endpoints require POST with no query."
        return None
    if re.fullmatch(rf"/memory-proposals/{UUID}/(?:revoke|supersede)", path):
        if method != "POST" or query_string:
            return 405, "Lifecycle writes require POST with no query."
        return None
    if not PATH.fullmatch(path):
        return 404, "Unsupported conversation endpoint."
    if method not in {"GET", "POST"}:
        return 405, "Unsupported method."

    listing = bool(re.fullmatch(
        rf"/conversations/{UUID}/memory-proposals", path
    ))
    reviewing = bool(re.fullmatch(
        rf"/conversations/{UUID}/memory-proposals/{UUID}/review", path
    ))

    if method == "POST":
        valid = (
            path == "/conversations"
            or path.endswith(("/messages", "/turns"))
            or reviewing
        )
        if not valid or query_string:
            return 405, "Unsupported write endpoint."
        return None

    if path.endswith("/turns") or reviewing:
        return 405, "Unsupported read endpoint."

    query = parse_qs(query_string, keep_blank_values=True)
    for key, values in query.items():
        if len(values) != 1:
            return 400, "Unsupported query."
        value = values[0]
        if key in {"limit", "offset"}:
            if not value.isascii() or not value.isdigit():
                return 400, "Unsupported query."
        elif key == "status" and listing:
            if value not in {"proposed", "accepted", "rejected"}:
                return 400, "Unsupported query."
        else:
            return 400, "Unsupported query."
    return None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, text):
        self.reply(code, json.dumps({"detail":text}).encode())

    def handle_request(self):
        hosts = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
        if self.headers.get("Host") not in hosts:
            return self.error(403, "Local host required.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            return self.error(400, "Invalid request target.")
        if self.command == "GET" and parsed.path in ("/", "/atlas-connected.js", "/atlas-project-memory.js"):
            name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            kind = "text/html" if name.endswith(".html") else "text/javascript"
            return self.reply(200, (ROOT / name).read_bytes(), kind+"; charset=utf-8")
        if not parsed.path.startswith("/api/"):
            return self.error(404, "Not found.")
        if self.headers.get("X-Atlas-Workspace") != "1":
            return self.error(403, "Workspace request required.")
        origin = self.headers.get("Origin")
        if origin and origin not in {
            f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"
        }:
            return self.error(403, "Cross-origin request rejected.")
        if parsed.path == "/api/workspace/context":
            if self.command != "GET":
                return self.error(405, "Read-only context endpoint.")
            args = parse_qs(parsed.query, keep_blank_values=True)
            if set(args) != {"conversation_id"} or len(args["conversation_id"]) != 1:
                return self.error(400, "One conversation ID required.")
            try:
                from workspace_context import context_status
                result = context_status(args["conversation_id"][0])
                return self.reply(200, json.dumps(result).encode("utf-8"))
            except (ValueError, UnicodeError):
                return self.error(400, "Invalid context request.")
            except (OSError, sqlite3.Error):
                return self.error(503, "Context storage unavailable.")
        from workspace_store import dispatch
        if dispatch(self, parsed):
            return
        path = parsed.path[4:]
        policy_error = conversation_request_error(
            self.command, path, parsed.query
        )
        if policy_error is not None:
            return self.error(*policy_error)
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding"):
                return self.error(400, "Transfer encoding not supported.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.error(400, "Invalid body length.")
            if not 0 < length <= 150000:
                return self.error(413, "Invalid or oversized request.")
            data = self.rfile.read(length)
            try:
                payload = json.loads(data)
                if not isinstance(payload, dict):
                    raise ValueError()
                from workspace_context import prepare_turn_body
                payload = prepare_turn_body(path, payload)
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except (ValueError, UnicodeError) as error:
                return self.error(400, str(error) or "Invalid request.")
            except (OSError, sqlite3.Error):
                return self.error(503, "Workspace context storage unavailable.")
        else:
            data = None
        request = Request(
            BACKEND + path + ("?"+parsed.query if parsed.query else ""),
            data=data, method=self.command,
            headers={
                "Authorization":"Bearer "+TOKEN,
                "Content-Type":"application/json"
            }
        )
        try:
            with opener.open(request, timeout=180) as response:
                if re.fullmatch(
                    rf"/conversations/{UUID}/voice-turns", path
                ):
                    return self.forward_voice_stream(response)
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    return self.error(502, "Backend response too large.")
                self.reply(response.status, body)
        except HTTPError as error:
            self.error(error.code, {
                401:"Atlas authentication rejected.",
                403:"Atlas permission denied.",
                404:"Conversation or message not found.",
                409:"Turn unavailable or already running. Refresh history before resuming.",
                422:"Atlas rejected the request format.",
                502:"Configured model failed. Pending message retained.",
                503:"Atlas service temporarily unavailable.",
                504:"Conversation execution timed out. Refresh history before resuming."
            }.get(error.code, "Atlas rejected the request."))
        except Exception:
            self.error(
                502,
                "Backend connection interrupted. Outcome may be unknown. "
                "Refresh history before resuming the pending message."
            )


    def forward_voice_stream(self, response):
        """Forward provisional events without buffering the full reply."""
        kind = response.headers.get_content_type()
        if kind != "application/x-ndjson":
            return self.error(502, "Backend did not return a voice event stream.")

        # Explicit close framing: no invented Content-Length.
        self.close_connection = True
        self.send_response(response.status)
        self.send_header(
            "Content-Type", "application/x-ndjson; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")

        try:
            self.end_headers()
            total = 0
            while True:
                chunk = response.read1(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > 8_000_000:
                    # Close without fabricating a successful terminal event.
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            # Headers may already be sent. Do not append an HTTP error
            # response to an event stream. Closing also closes upstream
            # when the caller leaves its response context manager.
            self.close_connection = True
        return

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
url = f"http://localhost:{PORT}"

def launch():
    try:
        powershell = shutil.which("powershell.exe")
        if powershell:
            subprocess.Popen(
                [powershell, "-NoProfile", "-Command", f"Start-Process '{url}'"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            webbrowser.open(url)
    except Exception:
        pass

print(f"\nCONNECTED: {BACKEND}", flush=True)
print(f"ATLAS WORKSPACE: {url}", flush=True)
print("Local development bridge only. Do not expose to the internet.", flush=True)
print("Keep this terminal running. Ctrl+C stops the workspace.", flush=True)
threading.Timer(0.8, launch).start()
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    httpd.server_close()
