from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4
import json, os, signal, socket, subprocess, sys, time

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "apps/atlas-workspace/run_workspace.py"
STATE = Path.home() / ".local/state/atlas-workspace"
PORT = 8766

def processes():
    found = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            args = [a.decode() for a in (p/"cmdline").read_bytes().split(b"\0") if a]
            if len(args) < 2 or not Path(args[0]).name.startswith("python"):
                continue
            script = Path(args[1])
            if not script.is_absolute():
                script = (p/"cwd").resolve() / script
            if script.resolve() == BRIDGE.resolve():
                found.append(int(p.name))
        except (OSError, UnicodeError):
            pass
    return found

def ready():
    base = f"http://127.0.0.1:{PORT}"
    with urlopen(base + "/atlas-connected.js", timeout=2) as r:
        if b"assistantProject" not in r.read(2000000):
            return False
    req = Request(
        base + "/api/workspace/context?conversation_id=" + str(uuid4()),
        headers={"X-Atlas-Workspace": "1"},
    )
    with urlopen(req, timeout=2) as r:
        data = json.loads(r.read(100000))
    return data.get("bound") is False and data.get("snapshot") is None

def main():
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action == "status":
        print("Workspace bridge processes:", len(processes()))
        try:
            print("Updated workspace HTTP:", "PASS" if ready() else "FAIL")
        except Exception:
            print("Updated workspace HTTP: unavailable")
        return
    if action != "restart":
        raise SystemExit("Use restart or status.")
    for pid in processes():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while processes() and time.monotonic() < deadline:
        time.sleep(0.2)
    if processes():
        raise SystemExit("STOP A4: previous bridge did not stop.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError:
            raise SystemExit("STOP A4: port 8766 occupied; no replacement started.")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE/"workspace.log").open("ab") as log:
        (STATE/"workspace.log").chmod(0o600)
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE)], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print("A4: starting workspace; checking for up to 90 seconds.", flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit("STOP A4: bridge exited; local workspace log recorded.")
        try:
            if ready():
                print("PASS A4: updated frontend and context endpoint respond.")
                print("ATLAS WORKSPACE: http://localhost:8766")
                print("DONE A4: running in background; terminal is free.")
                return
        except Exception:
            pass
        time.sleep(0.5)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise SystemExit("STOP A4: termination unconfirmed; do not relaunch.")
    raise SystemExit("STOP A4: startup checks timed out; new bridge stopped.")

if __name__ == "__main__":
    main()
