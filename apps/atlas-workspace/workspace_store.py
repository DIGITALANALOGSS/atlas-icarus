
from pathlib import Path
import json
import sqlite3
import uuid

DB = Path.home() / ".local/share/atlas-workspace/workspace.sqlite3"

def operate(method, route, data=None):
    data = data or {}
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(DB, timeout=10)
    try:
        DB.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        with db:
            if method == "GET" and route == "projects":
                return {"items": [dict(row) for row in db.execute(
                    "SELECT * FROM projects ORDER BY created_at,id"
                )]}
            if method == "GET" and route == "memories":
                return {"items": [dict(row) for row in db.execute("""
                    SELECT m.*, p.name AS project_name
                    FROM memories m LEFT JOIN projects p ON p.id=m.project_id
                    ORDER BY m.created_at,m.id
                """)]}
            if method != "POST":
                raise ValueError("Unsupported operation.")
            if route == "projects":
                name = data.get("name")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
                    raise ValueError("Project name must contain 1–200 characters.")
                identifier = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO projects(id,name) VALUES(?,?)",
                    (identifier, name.strip())
                )
                return {"id": identifier}
            if route == "memories":
                content = data.get("content")
                project = data.get("project_id") or None
                if not isinstance(content, str) or not 1 <= len(content.strip()) <= 20000:
                    raise ValueError("Memory must contain 1–20,000 characters.")
                if project is not None and (
                    not isinstance(project, str)
                    or not db.execute(
                        "SELECT id FROM projects WHERE id=?", (project,)
                    ).fetchone()
                ):
                    raise ValueError("Project does not exist.")
                identifier = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO memories(id,project_id,content) VALUES(?,?,?)",
                    (identifier, project, content.strip())
                )
                return {"id": identifier}
            if route == "forget":
                identifier = data.get("id")
                if not isinstance(identifier, str):
                    raise ValueError("Memory ID required.")
                cursor = db.execute(
                    "DELETE FROM memories WHERE id=?", (identifier,)
                )
                return {"deleted": cursor.rowcount == 1}
            raise ValueError("Unknown operation.")
    finally:
        db.close()

def dispatch(handler, parsed):
    prefix = "/api/workspace/"
    if not parsed.path.startswith(prefix):
        return False
    route = parsed.path[len(prefix):]
    if route not in {"projects", "memories", "forget"} or parsed.query:
        handler.error(404, "Unknown workspace endpoint.")
        return True
    try:
        data = {}
        if handler.command == "POST":
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer encoding unsupported.")
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 150000:
                raise ValueError("Invalid request size.")
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("JSON object required.")
        result = operate(handler.command, route, data)
        handler.reply(200, json.dumps(result).encode())
    except (ValueError, UnicodeError) as error:
        handler.error(400, str(error))
    except (OSError, sqlite3.Error):
        handler.error(503, "Workspace storage operation failed.")
    return True
