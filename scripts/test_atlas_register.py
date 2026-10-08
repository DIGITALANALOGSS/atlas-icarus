import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import atlas_preserve as preservation
import atlas_register as registration


class FakeClient:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
        self.intake_id = str(uuid4())
        self.evidence_id = str(uuid4())

    def request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if self.fail_at == len(self.calls):
            raise RuntimeError("private network detail")
        if path == "/intake-items":
            return {"intake_id": self.intake_id}
        return {"evidence_id": self.evidence_id}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "original.bin"
        self.source.write_bytes(b"synthetic\r\n\x00\xff")
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.preserved = preservation.preserve(
            self.source, self.storage,
            custodian="synthetic custodian",
            source_label="synthetic source",
            received_at="2026-10-08T14:00:00Z",
        )
        self.manifest = Path(self.preserved["manifest_path"])
        self.original_manifest = self.manifest.read_bytes()
        self.receipt = self.manifest.parent / "registration.json"

    def test_success_registers_only_metadata(self):
        client = FakeClient()
        result = registration.register(self.manifest, client)
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["intake_id"], client.intake_id)
        self.assertEqual(result["evidence_id"], client.evidence_id)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0][2]["original_sha256"], self.preserved["sha256"])
        self.assertEqual(
            client.calls[1][1],
            f"/intake-items/{client.intake_id}/evidence-records",
        )
        self.assertNotIn("content", client.calls[1][2])
        self.assertEqual(self.manifest.read_bytes(), self.original_manifest)
        self.assertNotIn("token", self.receipt.read_text())

    def test_changed_copy_prevents_all_requests(self):
        for key in ("working_copy", "preserved_copy"):
            path = Path(self.preserved[key])
            original = path.read_bytes()
            path.write_bytes(b"changed")
            client = FakeClient()
            with self.assertRaises(registration.RegistrationError):
                registration.register(self.manifest, client)
            self.assertEqual(client.calls, [])
            self.assertFalse(self.receipt.exists())
            path.write_bytes(original)

    def test_existing_receipt_prevents_retry(self):
        client = FakeClient()
        registration.register(self.manifest, client)
        second = FakeClient()
        with self.assertRaises(registration.RegistrationError):
            registration.register(self.manifest, second)
        self.assertEqual(second.calls, [])

    def test_intake_failure_records_uncertainty(self):
        client = FakeClient(fail_at=1)
        with self.assertRaises(registration.RegistrationError):
            registration.register(self.manifest, client)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["status"], "needs_review")
        self.assertEqual(receipt["last_stage"], "intake_request_pending")
        self.assertIsNone(receipt["intake_id"])
        self.assertNotIn("private network detail", self.receipt.read_text())

    def test_evidence_failure_retains_intake_identifier(self):
        client = FakeClient(fail_at=2)
        with self.assertRaises(registration.RegistrationError):
            registration.register(self.manifest, client)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["intake_id"], client.intake_id)
        self.assertIsNone(receipt["evidence_id"])
        self.assertEqual(receipt["last_stage"], "evidence_request_pending")
        self.assertEqual(self.manifest.read_bytes(), self.original_manifest)

    def test_failed_preservation_prevents_registration(self):
        (self.manifest.parent / "FAILED.json").write_text("{}")
        client = FakeClient()
        with self.assertRaises(registration.RegistrationError):
            registration.register(self.manifest, client)
        self.assertEqual(client.calls, [])

    def test_redirected_copy_path_rejected(self):
        data = json.loads(self.manifest.read_text())
        data["working_copy"] = str(self.source)
        self.manifest.write_text(json.dumps(data))
        client = FakeClient()
        with self.assertRaises(registration.RegistrationError):
            registration.register(self.manifest, client)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
