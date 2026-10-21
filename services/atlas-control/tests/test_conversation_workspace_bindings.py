import copy
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.conversation_workspace_bindings import (
    WorkspaceBindingError, ensure_workspace_binding,
)


class Connection:
    def __init__(self, principal, cid):
        self.principal = principal
        self.cid = cid
        self.owner = principal.subject_id
        self.tenant = principal.tenant_id
        self.correlation = uuid4()
        self.binding = None
        self.events = []
        self.fail_event = False
        self.queries = 0
        self.rollbacks = 0
        self.in_transaction = False

    @asynccontextmanager
    async def transaction(self):
        assert not self.in_transaction
        snapshot = copy.deepcopy((self.binding, self.events))
        self.in_transaction = True
        try:
            yield
        except BaseException:
            self.binding, self.events = snapshot
            self.rollbacks += 1
            raise
        finally:
            self.in_transaction = False

    async def fetchrow(self, sql, *args):
        assert self.in_transaction
        self.queries += 1
        sql = " ".join(sql.split())
        if "FROM conversations" in sql:
            assert "tenant_id = $2" in sql
            assert "owner_subject_id = $3" in sql
            assert "FOR UPDATE" in sql
            cid, tenant, owner = args
            if cid == self.cid and tenant == self.tenant and owner == self.owner:
                return {
                    "conversation_id": self.cid,
                    "correlation_id": self.correlation,
                }
            return None
        if "FROM conversation_workspace_bindings" in sql:
            assert "tenant_id = $1 AND conversation_id = $2" in sql
            assert args == (self.principal.tenant_id, self.cid)
            return copy.deepcopy(self.binding)
        if "INSERT INTO conversation_workspace_bindings" in sql:
            assert self.binding is None
            tenant, cid, scope, project, actor = args
            assert tenant == self.tenant and cid == self.cid
            self.binding = {
                "scope_type": scope,
                "workspace_project_id": project,
                "bound_by_subject_id": actor,
            }
            return copy.deepcopy(self.binding)
        raise AssertionError(f"Unexpected query: {sql}")


@pytest.fixture
def rig():
    principal = SimpleNamespace(
        tenant_id=uuid4(),
        subject_id="binding-test-owner",
        permissions={"conversations:read", "conversations:write"},
    )
    cid = uuid4()
    connection = Connection(principal, cid)

    async def event(conn, **fields):
        assert conn is connection
        assert conn.in_transaction
        if conn.fail_event:
            raise RuntimeError("simulated event failure")
        conn.events.append(copy.deepcopy(fields))
        return uuid4()

    return SimpleNamespace(
        principal=principal, cid=cid, connection=connection, event=event,
    )


async def bind(rig, project):
    return await ensure_workspace_binding(
        rig.connection, rig.principal, rig.cid, project,
        write_event=rig.event,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, uuid4()])
async def test_initial_binding_and_identical_retry(rig, project):
    first = await bind(rig, project)
    second = await bind(rig, project)
    expected_scope = "personal" if project is None else "project"
    assert first == second == {
        "scope_type": expected_scope,
        "workspace_project_id": project,
        "bound_by_subject_id": rig.principal.subject_id,
    }
    assert len(rig.connection.events) == 1
    event = rig.connection.events[0]
    assert event["tenant_id"] == rig.principal.tenant_id
    assert event["correlation_id"] == rig.connection.correlation
    assert event["event_type"] == "governance.conversation.workspace.bound"
    assert event["payload"] == {
        "conversation_id": str(rig.cid),
        "scope_type": expected_scope,
        "workspace_project_id": None if project is None else str(project),
        "bound_by_subject_id": rig.principal.subject_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("original,replacement", [
    (None, uuid4()),
    (uuid4(), None),
    (uuid4(), uuid4()),
])
async def test_binding_cannot_change(rig, original, replacement):
    saved = await bind(rig, original)
    with pytest.raises(WorkspaceBindingError, match="binding changed"):
        await bind(rig, replacement)
    assert rig.connection.binding == saved
    assert len(rig.connection.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [
    set(), {"conversations:read"}, {"conversations:write"},
])
async def test_both_permissions_required_before_database_access(rig, permissions):
    rig.principal.permissions = permissions
    with pytest.raises(WorkspaceBindingError, match="permission denied"):
        await bind(rig, None)
    assert rig.connection.queries == 0
    assert rig.connection.binding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner", "tenant"])
async def test_unrelated_conversation_is_hidden(rig, field):
    setattr(
        rig.connection, field,
        "another-owner" if field == "owner" else uuid4(),
    )
    with pytest.raises(WorkspaceBindingError, match="conversation unavailable"):
        await bind(rig, None)
    assert rig.connection.binding is None
    assert rig.connection.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["not-a-uuid", str(uuid4()), 123, {}])
async def test_unvalidated_project_values_rejected(rig, project):
    with pytest.raises(WorkspaceBindingError, match="UUID or null"):
        await bind(rig, project)
    assert rig.connection.queries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, uuid4()])
async def test_event_failure_rolls_back_initial_binding(rig, project):
    rig.connection.fail_event = True
    with pytest.raises(RuntimeError, match="simulated event failure"):
        await bind(rig, project)
    assert rig.connection.binding is None
    assert rig.connection.events == []
    assert rig.connection.rollbacks == 1
    rig.connection.fail_event = False
    assert await bind(rig, project)
    assert len(rig.connection.events) == 1
