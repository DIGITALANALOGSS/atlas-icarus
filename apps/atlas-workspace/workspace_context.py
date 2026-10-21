"""Single-user local memory retrieval for the existing workspace bridge."""
import hashlib
import json
import re
import sqlite3
from uuid import UUID
import workspace_store

TURN_PATH = re.compile(r"/conversations/([^/]+)/(?:turns|voice-turns)")


def canonical_uuid(value):
    if not isinstance(value, str):
        raise ValueError("A UUID string is required.")
    return str(UUID(value))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_turn_body(path, body):
    match = TURN_PATH.fullmatch(path)
    if match is None:
        return body

    if not isinstance(body, dict):
        raise ValueError("JSON object required.")
    if set(body) - {"user_message_id", "workspace_project_id"}:
        raise ValueError("Unsupported turn fields; memory is selected by the bridge.")

    conversation = canonical_uuid(match.group(1))
    message = canonical_uuid(body.get("user_message_id"))
    project = body.get("workspace_project_id")
    if project is not None:
        project = canonical_uuid(project)

    # Initialize existing storage through its established implementation.
    workspace_store.operate("GET", "projects")
    db = sqlite3.connect(workspace_store.DB, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_context_bindings (
            conversation_id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS workspace_context_snapshots (
            conversation_id TEXT NOT NULL,
            user_message_id TEXT NOT NULL,
            selection_json TEXT NOT NULL,
            omitted_count INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (conversation_id, user_message_id)
        );
        """)
        db.execute("BEGIN IMMEDIATE")

        if project is not None and db.execute(
            "SELECT 1 FROM projects WHERE id=?", (project,)
        ).fetchone() is None:
            raise ValueError("Selected project does not exist.")

        binding = db.execute(
            "SELECT project_id FROM workspace_context_bindings "
            "WHERE conversation_id=?", (conversation,)
        ).fetchone()
        if binding is None:
            db.execute(
                "INSERT INTO workspace_context_bindings "
                "(conversation_id, project_id) VALUES (?,?)",
                (conversation, project),
            )
        elif binding["project_id"] != project:
            raise ValueError(
                "Conversation project differs from its saved association. "
                "Use its original project or create a new conversation."
            )

        snapshot = db.execute(
            "SELECT selection_json, omitted_count "
            "FROM workspace_context_snapshots "
            "WHERE conversation_id=? AND user_message_id=?",
            (conversation, message),
        ).fetchone()

        if snapshot is not None:
            selected = json.loads(snapshot["selection_json"])
            for item in selected:
                current = db.execute(
                    "SELECT content, project_id FROM memories WHERE id=?",
                    (item["memory_id"],),
                ).fetchone()
                if (
                    current is None
                    or current["project_id"] != item["project_id"]
                    or digest(current["content"]) != item["content_sha256"]
                ):
                    raise ValueError(
                        "Saved context changed or was deleted. "
                        "This turn will not be resent with obsolete memory. "
                        "Refresh conversation history before continuing."
                    )
        else:
            rows = list(db.execute(
                "SELECT id, project_id, content FROM memories "
                "WHERE project_id IS NULL OR project_id=? "
                "ORDER BY created_at DESC, id DESC",
                (project,),
            ))
            personal = [row for row in rows if row["project_id"] is None]
            project_rows = [row for row in rows if row["project_id"] is not None]
            chosen = (
                personal[:12] if project is None
                else personal[:6] + project_rows[:6]
            )
            selected = [
                {
                    "memory_id": row["id"],
                    "project_id": row["project_id"],
                    "content_sha256": digest(row["content"]),
                    "excerpt": row["content"][:1000],
                    "shortened": len(row["content"]) > 1000,
                }
                for row in chosen
            ]
            db.execute(
                "INSERT INTO workspace_context_snapshots "
                "(conversation_id,user_message_id,selection_json,omitted_count) "
                "VALUES (?,?,?,?)",
                (
                    conversation,
                    message,
                    json.dumps(selected, ensure_ascii=False),
                    len(rows) - len(selected),
                ),
            )

        db.commit()
        result = {
            "user_message_id": message,
            "memory_context": [item["excerpt"] for item in selected],
        }
        if "workspace_project_id" in body:
            result["workspace_project_id"] = project
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def context_status(conversation_id):
    """Read context metadata without returning saved memory content."""
    conversation = canonical_uuid(conversation_id)
    path = workspace_store.DB
    result = {"bound": False, "project_id": None, "snapshot": None}
    if not path.is_file():
        return result
    db = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
    )
    db.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "workspace_context_bindings" in tables:
            row = db.execute(
                "SELECT project_id FROM workspace_context_bindings "
                "WHERE conversation_id=?", (conversation,)
            ).fetchone()
            if row:
                result.update(bound=True, project_id=row["project_id"])
        if "workspace_context_snapshots" in tables:
            row = db.execute(
                "SELECT selection_json, omitted_count "
                "FROM workspace_context_snapshots WHERE conversation_id=? "
                "ORDER BY rowid DESC LIMIT 1", (conversation,)
            ).fetchone()
            if row:
                records = json.loads(row["selection_json"])
                result["snapshot"] = {
                    "selected_count": len(records),
                    "shortened_count": sum(
                        bool(r.get("shortened")) for r in records
                    ),
                    "omitted_count": row["omitted_count"],
                }
        return result
    finally:
        db.close()
