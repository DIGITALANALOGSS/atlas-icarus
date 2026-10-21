import asyncio
import json
import unittest
from uuid import uuid4
from app.conversation_learning import extract_candidates

TEXT = "I prefer spoken explanations before terminal commands."

class FakeModel:
    def __init__(self, raw):
        self.raw = raw

    async def complete(self, messages):
        self.messages = messages
        return self.raw

class LearningTests(unittest.TestCase):
    def record(self, **changes):
        item = dict(category="communication_preference", quote=TEXT)
        item.update(changes)
        return item

    def extract(self, items):
        model = FakeModel(json.dumps({"memories": items}))
        cid, mid = str(uuid4()), str(uuid4())
        rows = asyncio.run(extract_candidates(
            model, user_text=TEXT,
            conversation_id=cid, message_id=mid,
        ))
        return rows, model, cid, mid

    def test_source_linked_proposal(self):
        rows, model, cid, mid = self.extract([self.record()])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], TEXT)
        self.assertEqual(rows[0]["status"], "proposed")
        self.assertEqual(rows[0]["source_conversation_id"], cid)
        self.assertEqual(rows[0]["source_message_id"], mid)
        self.assertEqual(
            json.loads(model.messages[1].content)["user_text"], TEXT
        )

    def test_invented_quote_rejected(self):
        with self.assertRaises(ValueError):
            self.extract([self.record(quote="Invented personal fact")])

    def test_extra_authority_field_rejected(self):
        with self.assertRaises(ValueError):
            self.extract([self.record(permissions=["execute"])])

    def test_duplicates_collapsed(self):
        rows, _, _, _ = self.extract([self.record(), self.record()])
        self.assertEqual(len(rows), 1)

    def test_empty_allowed(self):
        self.assertEqual(self.extract([])[0], [])

    def test_excess_records_rejected(self):
        with self.assertRaises(ValueError):
            self.extract([self.record()] * 4)

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError):
            self.extract([self.record(category="tool_permission")])

    def test_invalid_json_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(extract_candidates(
                FakeModel("not JSON"), user_text=TEXT,
                conversation_id=uuid4(), message_id=uuid4(),
            ))

    def test_timeout(self):
        class SlowModel:
            async def complete(self, messages):
                await asyncio.sleep(1)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(extract_candidates(
                SlowModel(), user_text=TEXT,
                conversation_id=uuid4(), message_id=uuid4(),
                timeout_seconds=0.01,
            ))
