import copy
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.conversation_memory_review_routes import (
    register_conversation_memory_review_routes,
)

TENANT = UUID("10000000-0000-0000-0000-000000000001")
CONVERSATION = UUID("20000000-0000-0000-0000-000000000001")
PROPOSAL = UUID("30000000-0000-0000-0000-000000000001")
SUBJECT = "review-test-owner"
BASE = f"/conversations/{CONVERSATION}/memory-proposals"
REVIEW = f"{BASE}/{PROPOSAL}/review"


def proposal(status="proposed", **overrides):
    reviewed = status != "proposed"
    row = {
        "proposal_id": PROPOSAL,
        "tenant_id": TENANT,
        "conversation_id": CONVERSATION,
        "source_message_id": uuid4(),
        "job_id": uuid4(),
        "category": "accessibility_preference",
        "content": "Provide complete copy-and-paste commands.",
        "status": status,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "reviewed_at": (
            datetime(2026, 1, 2, tzinfo=timezone.utc) if reviewed else None
        ),
        "reviewed_by_subject_id": SUBJECT if reviewed else None,
        "memory_scope": "conversation",
        "workspace_project_id": None,
    }
    row.update(overrides)
    return row


def public_row(row):
    return {key: value for key, value in row.items() if key != "tenant_id"}


class Connection:
    def __init__(self):
        self.conversation = {
            "conversation_id": CONVERSATION,
            "tenant_id": TENANT,
            "owner_subject_id": SUBJECT,
            "correlation_id": uuid4(),
        }
        self.rows = [proposal()]
        self.events = []
        self.fail_event = False
        self.workspace_binding = None
        self.in_transaction = False
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self):
        assert not self.in_transaction
        snapshot = copy.deepcopy((self.rows, self.events))
        self.in_transaction = True
        try:
            yield
        except BaseException:
            self.rows, self.events = snapshot
            self.rollbacks += 1
            raise
        finally:
            self.in_transaction = False

    async def fetchrow(self, sql, *args):
        assert self.in_transaction
        sql = " ".join(sql.split())
        if "FROM conversations" in sql:
            assert "tenant_id = $2" in sql
            assert "owner_subject_id = $3" in sql
            assert "FOR SHARE" in sql or "FOR UPDATE" in sql
            conversation_id, tenant_id, subject_id = args
            row = self.conversation
            if row is not None and (
                row["conversation_id"] == conversation_id
                and row["tenant_id"] == tenant_id
                and row["owner_subject_id"] == subject_id
            ):
                return copy.deepcopy(row)
            return None

        if "FROM conversation_workspace_bindings" in sql:
            assert "tenant_id = $1" in sql
            assert "conversation_id = $2" in sql
            assert "scope_type = 'project'" in sql
            assert "workspace_project_id = $3" in sql
            tenant_id, conversation_id, project_id = args
            if (
                tenant_id == self.conversation["tenant_id"]
                and conversation_id == self.conversation["conversation_id"]
                and self.workspace_binding is not None
                and self.workspace_binding["workspace_project_id"] == project_id
            ):
                return copy.deepcopy(self.workspace_binding)
            return None

        if sql.startswith("SELECT"):
            assert "FOR UPDATE OF p" in sql
            assert "p.proposal_id = $1" in sql
            assert "p.conversation_id = $2" in sql
            assert "p.tenant_id = $3" in sql
            proposal_id, conversation_id, tenant_id = args
            for row in self.rows:
                if (
                    row["proposal_id"] == proposal_id
                    and row["conversation_id"] == conversation_id
                    and row["tenant_id"] == tenant_id
                ):
                    return public_row(copy.deepcopy(row))
            return None

        if sql.startswith("UPDATE conversation_memory_proposals"):
            assert "tenant_id = $3" in sql
            assert "conversation_id = $2" in sql
            assert "status = 'proposed'" in sql
            (
                proposal_id, conversation_id, tenant_id, decision,
                subject_id, memory_scope, project_id,
            ) = args
            for row in self.rows:
                if (
                    row["proposal_id"] == proposal_id
                    and row["conversation_id"] == conversation_id
                    and row["tenant_id"] == tenant_id
                    and row["status"] == "proposed"
                ):
                    row.update(
                        status=decision,
                        reviewed_at=datetime.now(timezone.utc),
                        reviewed_by_subject_id=subject_id,
                        memory_scope=memory_scope,
                        workspace_project_id=project_id,
                    )
                    return public_row(copy.deepcopy(row))
            return None
        raise AssertionError(f"Unexpected query: {sql}")

    async def fetch(self, sql, *args):
        assert self.in_transaction
        sql = " ".join(sql.split())
        assert "p.conversation_id = $1" in sql
        assert "p.tenant_id = $2" in sql
        assert "p.status = $3" in sql
        assert "ORDER BY p.created_at, p.proposal_id" in sql
        assert "LIMIT $4 OFFSET $5" in sql
        conversation_id, tenant_id, state, limit, offset = args
        rows = [
            row for row in self.rows
            if row["conversation_id"] == conversation_id
            and row["tenant_id"] == tenant_id
            and row["status"] == state
        ]
        rows.sort(key=lambda row: (row["created_at"], row["proposal_id"]))
        return [
            public_row(copy.deepcopy(row))
            for row in rows[offset:offset + limit]
        ]


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def rig():
    connection = Connection()
    app = FastAPI()
    app.state.pool = Pool(connection)

    async def write_event(conn, **fields):
        assert conn is connection
        assert conn.in_transaction
        if conn.fail_event:
            raise RuntimeError("simulated event failure")
        conn.events.append(copy.deepcopy(fields))
        return uuid4()

    register_conversation_memory_review_routes(app, write_event)

    principal = SimpleNamespace(
        tenant_id=TENANT,
        subject_id=SUBJECT,
        permissions={"conversations:read", "conversations:write"},
    )

    async def provide_principal():
        return principal

    for route in app.routes:
        if getattr(route, "path", None) == BASE.replace(
            str(CONVERSATION), "{conversation_id}"
        ):
            dependency = route.dependant.dependencies[0]
            app.dependency_overrides[dependency.call] = provide_principal
        elif getattr(route, "path", None) == (
            "/conversations/{conversation_id}/memory-proposals/"
            "{proposal_id}/review"
        ):
            writer = route.dependant.dependencies[0]
            permission_dependency = writer.dependencies[0]
            app.dependency_overrides[
                permission_dependency.call
            ] = provide_principal

    return SimpleNamespace(
        app=app, connection=connection, principal=principal
    )


async def request(rig, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("GET", BASE, None),
    ("POST", REVIEW, {"decision": "accepted"}),
])
@pytest.mark.parametrize("headers,expected", [
    ({}, 401),
    ({"Authorization": "Bearer dev-worker"}, 403),
])
async def test_actual_authentication_gates(
    rig, method, path, body, headers, expected
):
    rig.app.dependency_overrides.clear()
    rig.app.state.pool = None
    response = await request(
        rig, method, path, json=body, headers=headers
    )
    assert response.status_code == expected
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_review_also_requires_read_permission(rig):
    rig.principal.permissions = {"conversations:write"}
    response = await request(
        rig, "POST", REVIEW, json={"decision": "accepted"}
    )
    assert response.status_code == 403
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["proposed", "accepted", "rejected"])
async def test_listing_filters_status_and_scope(rig, state):
    rig.connection.rows = [
        proposal(),
        proposal("accepted", proposal_id=uuid4()),
        proposal("rejected", proposal_id=uuid4()),
        proposal(state, tenant_id=uuid4(), proposal_id=uuid4()),
        proposal(state, conversation_id=uuid4(), proposal_id=uuid4()),
    ]
    params = {} if state == "proposed" else {"status": state}
    response = await request(rig, "GET", BASE, params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == state
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == state
    assert "tenant_id" not in body["items"][0]
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_listing_pagination(rig):
    rig.connection.rows = [
        proposal(proposal_id=UUID(int=i)) for i in (1, 2, 3)
    ]
    response = await request(
        rig, "GET", BASE, params={"limit": 1, "offset": 1}
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["proposal_id"] for row in body["items"]] == [
        str(UUID(int=2))
    ]
    assert body["limit"] == 1
    assert body["offset"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "owner_subject_id"])
@pytest.mark.parametrize("method,path,body", [
    ("GET", BASE, None),
    ("POST", REVIEW, {"decision": "accepted"}),
])
async def test_unrelated_conversation_is_hidden(
    rig, field, method, path, body
):
    rig.connection.conversation[field] = (
        uuid4() if field == "tenant_id" else "another-owner"
    )
    response = await request(rig, method, path, json=body)
    assert response.status_code == 404
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "rejected"])
async def test_review_records_decision_and_event(rig, decision):
    response = await request(
        rig, "POST", REVIEW, json={"decision": decision}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == decision
    assert body["reviewed_by_subject_id"] == SUBJECT
    assert body["reviewed_at"] is not None
    assert len(rig.connection.events) == 1
    event = rig.connection.events[0]
    assert event["tenant_id"] == TENANT
    assert event["correlation_id"] == (
        rig.connection.conversation["correlation_id"]
    )
    assert event["event_type"] == "governance.conversation.memory_proposal.reviewed"
    assert event["payload"] == {
        "conversation_id": str(CONVERSATION),
        "proposal_id": str(PROPOSAL),
        "source_message_id": str(
            rig.connection.rows[0]["source_message_id"]
        ),
        "job_id": str(rig.connection.rows[0]["job_id"]),
        "decision": decision,
        "memory_scope": "conversation",
        "workspace_project_id": None,
        "reviewed_by_subject_id": SUBJECT,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "rejected"])
async def test_same_decision_retry_is_idempotent(rig, decision):
    first = await request(
        rig, "POST", REVIEW, json={"decision": decision}
    )
    second = await request(
        rig, "POST", REVIEW, json={"decision": decision}
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(rig.connection.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("original,opposite", [
    ("accepted", "rejected"),
    ("rejected", "accepted"),
])
async def test_opposite_decision_conflicts(rig, original, opposite):
    rig.connection.rows = [proposal(original)]
    before = copy.deepcopy(rig.connection.rows)
    response = await request(
        rig, "POST", REVIEW, json={"decision": opposite}
    )
    assert response.status_code == 409
    assert rig.connection.rows == before
    assert rig.connection.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {},
    {"decision": "proposed"},
    {"decision": "accepted", "reviewed_by_subject_id": "forged"},
])
async def test_invalid_review_body_is_rejected(rig, body):
    response = await request(rig, "POST", REVIEW, json=body)
    assert response.status_code == 422
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "conversation_id"])
async def test_unrelated_proposal_is_hidden(rig, field):
    rig.connection.rows[0][field] = uuid4()
    response = await request(
        rig, "POST", REVIEW, json={"decision": "accepted"}
    )
    assert response.status_code == 404
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_missing_proposal_is_hidden(rig):
    rig.connection.rows = []
    response = await request(
        rig, "POST", REVIEW, json={"decision": "accepted"}
    )
    assert response.status_code == 404
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_event_failure_rolls_back_in_transaction_double(rig):
    before = copy.deepcopy(rig.connection.rows)
    rig.connection.fail_event = True
    response = await request(
        rig, "POST", REVIEW, json={"decision": "accepted"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "database write failed"
    assert rig.connection.rows == before
    assert rig.connection.events == []
    assert rig.connection.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"decision": "rejected", "memory_scope": "personal"},
    {"decision": "accepted", "memory_scope": "project"},
    {
        "decision": "accepted", "memory_scope": "personal",
        "workspace_project_id": str(CONVERSATION),
    },
    {
        "decision": "accepted", "memory_scope": "conversation",
        "workspace_project_id": str(CONVERSATION),
    },
    {"decision": "accepted", "memory_scope": "unknown"},
])
async def test_invalid_scope_request_is_rejected(rig, body):
    response = await request(rig, "POST", REVIEW, json=body)
    assert response.status_code == 422
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_explicit_personal_acceptance_records_scope(rig):
    body = {"decision": "accepted", "memory_scope": "personal"}
    first = await request(rig, "POST", REVIEW, json=body)
    assert first.status_code == 200
    assert first.json()["memory_scope"] == "personal"
    assert first.json()["workspace_project_id"] is None
    second = await request(rig, "POST", REVIEW, json=body)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(rig.connection.events) == 1
    event = rig.connection.events[0]["payload"]
    assert event["memory_scope"] == "personal"
    assert event["workspace_project_id"] is None


@pytest.mark.asyncio
async def test_project_acceptance_requires_matching_binding(rig):
    project = uuid4()
    rig.connection.workspace_binding = {"workspace_project_id": project}
    body = {
        "decision": "accepted",
        "memory_scope": "project",
        "workspace_project_id": str(project),
    }
    first = await request(rig, "POST", REVIEW, json=body)
    assert first.status_code == 200
    assert first.json()["memory_scope"] == "project"
    assert first.json()["workspace_project_id"] == str(project)
    second = await request(rig, "POST", REVIEW, json=body)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(rig.connection.events) == 1
    event = rig.connection.events[0]["payload"]
    assert event["memory_scope"] == "project"
    assert event["workspace_project_id"] == str(project)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_other_binding", [False, True])
async def test_project_acceptance_fails_closed_without_matching_binding(
    rig, has_other_binding
):
    if has_other_binding:
        rig.connection.workspace_binding = {"workspace_project_id": uuid4()}
    response = await request(rig, "POST", REVIEW, json={
        "decision": "accepted",
        "memory_scope": "project",
        "workspace_project_id": str(uuid4()),
    })
    assert response.status_code == 409
    assert rig.connection.rows[0]["status"] == "proposed"
    assert rig.connection.events == []


@pytest.mark.asyncio
async def test_finalized_scope_cannot_be_silently_broadened(rig):
    first = await request(
        rig, "POST", REVIEW, json={"decision": "accepted"}
    )
    assert first.status_code == 200
    second = await request(rig, "POST", REVIEW, json={
        "decision": "accepted", "memory_scope": "personal",
    })
    assert second.status_code == 409
    assert rig.connection.rows[0]["memory_scope"] == "conversation"
    assert len(rig.connection.events) == 1


@pytest.mark.asyncio
async def test_scope_and_decision_roll_back_together_on_event_failure(rig):
    before = copy.deepcopy(rig.connection.rows)
    rig.connection.fail_event = True
    response = await request(rig, "POST", REVIEW, json={
        "decision": "accepted", "memory_scope": "personal",
    })
    assert response.status_code == 503
    assert rig.connection.rows == before
    assert rig.connection.events == []
    assert rig.connection.rollbacks == 1
