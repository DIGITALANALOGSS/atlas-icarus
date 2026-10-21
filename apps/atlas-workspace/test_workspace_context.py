import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
import workspace_store
from workspace_context import prepare_turn_body


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = workspace_store.DB
        workspace_store.DB = Path(self.temp.name) / "workspace.sqlite3"
        self.project = workspace_store.operate(
            "POST", "projects", {"name": "Project A"}
        )["id"]
        self.other = workspace_store.operate(
            "POST", "projects", {"name": "Project B"}
        )["id"]
        self.personal = self.save(None, "Personal preference")
        self.save(self.project, "Project A fact")
        self.save(self.other, "Project B private fact")
        self.path = "/conversations/" + str(uuid4()) + "/turns"

    def tearDown(self):
        workspace_store.DB = self.previous
        self.temp.cleanup()

    def save(self, project, content):
        return workspace_store.operate(
            "POST", "memories",
            {"project_id": project, "content": content},
        )["id"]

    def body(self, project=None):
        return {
            "user_message_id": str(uuid4()),
            "workspace_project_id": project,
        }

    def test_personal_only(self):
        result = prepare_turn_body(self.path, self.body())
        self.assertEqual(result["memory_context"], ["Personal preference"])

    def test_project_isolation(self):
        result = prepare_turn_body(self.path, self.body(self.project))
        self.assertEqual(
            set(result["memory_context"]),
            {"Personal preference", "Project A fact"},
        )

    def test_retry_is_stable_after_new_memory(self):
        body = self.body(self.project)
        first = prepare_turn_body(self.path, body)
        self.save(self.project, "Newly saved fact")
        self.assertEqual(first, prepare_turn_body(self.path, body))

    def test_deleted_memory_blocks_retry(self):
        body = self.body()
        prepare_turn_body(self.path, body)
        workspace_store.operate("POST", "forget", {"id": self.personal})
        with self.assertRaises(ValueError):
            prepare_turn_body(self.path, body)

    def test_project_cannot_silently_change(self):
        prepare_turn_body(self.path, self.body(self.project))
        with self.assertRaises(ValueError):
            prepare_turn_body(self.path, self.body(self.other))

    def test_missing_project_rejected(self):
        with self.assertRaises(ValueError):
            prepare_turn_body(self.path, self.body(str(uuid4())))

    def test_client_memory_rejected(self):
        body = self.body()
        body["memory_context"] = ["Client-supplied override"]
        with self.assertRaises(ValueError):
            prepare_turn_body(self.path, body)

    def test_limits_and_selection_ledger(self):
        for number in range(15):
            self.save(None, str(number) + "x" * 1100)
        result = prepare_turn_body(self.path, self.body())
        self.assertEqual(len(result["memory_context"]), 12)
        self.assertTrue(all(len(item) <= 1000 for item in result["memory_context"]))
        with sqlite3.connect(workspace_store.DB) as db:
            row = db.execute(
                "SELECT selection_json, omitted_count "
                "FROM workspace_context_snapshots"
            ).fetchone()
        self.assertEqual(row[1], 4)
        self.assertTrue(any(item["shortened"] for item in json.loads(row[0])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
