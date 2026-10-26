"""Start a saved Granite candidate with bounded readiness monitoring."""
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

def available_kib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("Available-memory measurement is unavailable")

def main():
    os.umask(0o077)
    if len(sys.argv) != 2:
        raise RuntimeError("Pass exactly one private candidate JSON path")

    candidate_path = Path(sys.argv[1]).expanduser().resolve()
    settings = json.loads(candidate_path.read_text())
    if (
        settings["alias"] != "atlas-granite-test"
        or settings["port"] != 8081
        or settings["context_size"] != 8192
        or settings["parallel"] != 1
    ):
        raise RuntimeError("Candidate differs from the approved startup target")

    for field in ("binary", "model", "key_file"):
        if not Path(settings[field]).is_file():
            raise RuntimeError("A required runtime file is missing")

    if not os.access(settings["binary"], os.X_OK):
        raise RuntimeError("Runtime binary is not executable")
    if Path(settings["key_file"]).stat().st_size == 0:
        raise RuntimeError("Key file is empty")

    lock = (candidate_path.parent / "startup.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another startup is already in progress") from None

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.joinpath("comm").read_text().strip() != "llama-server":
                continue
            if b"granite-4.0-h-tiny" in entry.joinpath("cmdline").read_bytes():
                raise RuntimeError("Granite is already running; no duplicate started")
        except OSError:
            continue

    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", settings["port"]))
        except OSError:
            raise RuntimeError("Port 8081 is already occupied") from None

    free = available_kib()
    print(f"Available memory before launch: {free // 1024} MiB", flush=True)
    if free < 10 * 1024 * 1024:
        raise RuntimeError("Less than 10 GiB available; launch withheld for review")

    run_dir = candidate_path.parent / (
        "startup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + "-" + str(os.getpid())
    )
    run_dir.mkdir(mode=0o700)
    log_path = run_dir / "server.log"

    command = [
        settings["binary"],
        "--model", settings["model"],
        "--alias", settings["alias"],
        "--host", "127.0.0.1",
        "--port", str(settings["port"]),
        "--ctx-size", str(settings["context_size"]),
        "--parallel", str(settings["parallel"]),
        "--threads", str(settings["cpu_threads"]),
        "--n-gpu-layers", "0",
        "--api-key-file", settings["key_file"],
    ]

    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("LLAMA_ARG_") and key != "LLAMA_API_KEY"
    }

    print("Starting Granite: context 8192; port 8081.", flush=True)
    print("Loading may take several minutes. Progress every 10 seconds.", flush=True)
    print("LOG:", log_path, flush=True)

    with log_path.open("xb") as log:
        process = subprocess.Popen(
            command,
            cwd=str(Path.home()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    try:
        record = {
            "pid": process.pid,
            "candidate": str(candidate_path),
            "context_size_requested": 8192,
            "port": 8081,
            "log_file": str(log_path),
            "status": "starting",
        }
        record_path = run_dir / "running.json"
        record_path.write_text(json.dumps(record, indent=2))

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        started = time.monotonic()
        next_progress = 0

        while time.monotonic() - started < 300:
            if process.poll() is not None:
                raise RuntimeError(
                    "Granite exited during startup; exit code "
                    + str(process.returncode)
                )

            free = available_kib()
            if free < 1024 * 1024:
                raise RuntimeError("Available memory fell below 1 GiB")

            try:
                with opener.open(
                    "http://127.0.0.1:8081/health", timeout=2
                ) as response:
                    ready = (
                        response.status == 200
                        and json.loads(response.read(4096)).get("status") == "ok"
                    )
            except Exception:
                ready = False

            if ready and process.poll() is None:
                record["status"] = "ready"
                record_path.write_text(json.dumps(record, indent=2))
                print("READY: Granite health check passed.", flush=True)
                print("PID:", process.pid, flush=True)
                print("REQUESTED_CONTEXT: 8192", flush=True)
                print("RUNTIME_RECORD:", record_path, flush=True)
                print(
                    "No generation performed; context capacity still needs qualification.",
                    flush=True,
                )
                return

            elapsed = time.monotonic() - started
            if elapsed >= next_progress:
                print(
                    f"Loading: {int(elapsed)} seconds; "
                    f"available memory {free // 1024} MiB",
                    flush=True,
                )
                next_progress = elapsed + 10
            time.sleep(2)

        raise RuntimeError("Startup exceeded the five-minute deadline")

    except BaseException:
        if process.poll() is None:
            print("Stopping only the Granite process launched here...", flush=True)
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("STOP: startup monitoring interrupted.", flush=True)
        sys.exit(1)
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("STOP:", message, flush=True)
        sys.exit(1)
