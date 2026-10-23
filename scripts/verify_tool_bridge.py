"""Run container-based checks; save output and show progress."""
import datetime
from pathlib import Path
import subprocess

def main():
    repo = Path(__file__).resolve().parents[1]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    report = (
        Path.home() / ".local/state/atlas-model-qualification"
        / ("bridge-concurrency-" + stamp)
    )
    report.mkdir(parents=True, mode=0o700)
    log = report / "pytest.log"
    name = "atlas-bridge-race-" + stamp.lower()
    command = [
        "docker", "compose", "-f", str(repo / "compose.yaml"),
        "--profile", "integration", "run", "--rm", "-T",
        "--name", name, "atlas-control-integration-tests",
        "python", "-B", "-m", "pytest", "-q", "-ra",
        "-p", "no:cacheprovider", "tests", "tests/integration",
    ]
    print("Running container-based checks. Limit: 180 seconds.",
          flush=True)
    print("Full log:", log, flush=True)
    with log.open("w") as handle:
        process = subprocess.Popen(
            command, cwd=repo, stdout=handle, stderr=subprocess.STDOUT,
        )
        status = 124
        try:
            for elapsed in range(15, 181, 15):
                try:
                    status = process.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    print(f"Still running: about {elapsed} seconds.", flush=True)
        finally:
            if process.poll() is None:
                print("Stopping only this test runner.", flush=True)
                try:
                    subprocess.run(
                        ["docker", "stop", "-t", "5", name],
                        stdout=handle, stderr=subprocess.STDOUT, timeout=20,
                    )
                except subprocess.TimeoutExpired:
                    handle.write("\nTest-runner stop command timed out.\n")
                finally:
                    process.kill()
                    process.wait()
    print("\n=== FINAL SUMMARY ===")
    print("\n".join(log.read_text(errors="replace").splitlines()[-30:]))
    print("\nExit code:", status)
    print("Evidence:", log)
    print("No application edits, deployment, model calls, or job execution.")
    return status

if __name__ == "__main__":
    raise SystemExit(main())
