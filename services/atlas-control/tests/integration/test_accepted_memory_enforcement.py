from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

import app.conversation_coordinator as coordinator
from app.auth import development_principals
from app.conversation_memory import stored_memory_context
from app.conversation_memory_recall import select_accepted_memories
from app.main import app, write_event
from test_workspace_connected_postgres import workspace_case


@pytest_asyncio.fixture
async def recall_case(workspace_case, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    foreign_tenant = development_principals()[
        "dev-cross-tenant-operator"
    ].tenant_id
    await workspace_case["connection"].execute(
        "INSERT INTO tenants (tenant_id, slug) VALUES ($1, $2) "
        "ON CONFLICT (tenant_id) DO NOTHING",
        foreign_tenant, "integration-recall-" + foreign_tenant.hex,
    )
    return workspace_case


async def new_source(case, *, owner=None, tenant=None):
    principal = case["principal"]
    tenant = principal.tenant_id if tenant is None else tenant
    owner = principal.subject_id if owner is None else owner
    cid, mid = uuid4(), uuid4()
    connection = case["connection"]
    await connection.execute(
        "INSERT INTO conversations "
        "(conversation_id,tenant_id,owner_subject_id,correlation_id) "
        "VALUES ($1,$2,$3,$1)",
        cid, tenant, owner,
    )
    await connection.execute(
        "INSERT INTO conversation_messages "
        "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
        "VALUES ($1,$2,$3,1,'user','Memory enforcement fixture')",
        mid, tenant, cid,
    )
    return tenant, cid, mid


async def bind_source(case, source, project):
    tenant, cid, _ = source
    await case["connection"].execute(
        "INSERT INTO conversation_workspace_bindings "
        "(tenant_id,conversation_id,scope_type,workspace_project_id,"
        "bound_by_subject_id) VALUES ($1,$2,$3,$4,$5)",
        tenant, cid, "personal" if project is None else "project",
        project, case["principal"].subject_id,
    )


async def proposal(
    case, source, content, *,
    scope="conversation", status="accepted", project=None, reviewer=None,
):
    tenant, cid, mid = source
    connection = case["connection"]
    job = await connection.fetchval(
        "INSERT INTO conversation_learning_jobs "
        "(job_id,tenant_id,conversation_id,source_message_id) "
        "VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (tenant_id,conversation_id,source_message_id) "
        "DO UPDATE SET source_message_id=EXCLUDED.source_message_id "
        "RETURNING job_id",
        uuid4(), tenant, cid, mid,
    )
    pid = uuid4()
    reviewer = case["principal"].subject_id if reviewer is None else reviewer
    await connection.execute(
        """
        INSERT INTO conversation_memory_proposals (
            proposal_id,tenant_id,conversation_id,source_message_id,job_id,
            category,content,status,reviewed_at,reviewed_by_subject_id,
            memory_scope,workspace_project_id
        )
        VALUES (
            $1,$2,$3,$4,$5,'constraint',$6,$7,
            CASE WHEN $7='proposed' THEN NULL ELSE NOW() END,
            CASE WHEN $7='proposed' THEN NULL ELSE $8::text END,
            $9,$10
        )
        """,
        pid, tenant, cid, mid, job, content, status, reviewer, scope, project,
    )
    return pid


def target_source(case):
    return case["principal"].tenant_id, case["cid"], case["user"]


async def reserve(case, **kwargs):
    return await coordinator.reserve_turn(
        case["pool"], case["principal"], case["cid"], case["user"],
        write_event=write_event, **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["unbound", "personal", "project"])
async def test_postgres_scope_and_identity_enforcement(recall_case, binding):
    case = recall_case
    principal = case["principal"]
    project, other_project = uuid4(), uuid4()
    target = target_source(case)

    await proposal(case, target, "Current conversation accepted")
    await proposal(case, target, "Pending must not appear", status="proposed")
    await proposal(case, target, "Rejected must not appear", status="rejected")

    personal = await new_source(case)
    await proposal(case, personal, "Owner personal accepted", scope="personal")
    await proposal(case, personal, "Other conversation must not appear")

    project_source = await new_source(case)
    await bind_source(case, project_source, project)
    await proposal(
        case, project_source, "Matching project accepted",
        scope="project", project=project,
    )

    other_source = await new_source(case)
    await bind_source(case, other_source, other_project)
    await proposal(
        case, other_source, "Wrong project must not appear",
        scope="project", project=other_project,
    )

    foreign_owner = await new_source(case, owner="foreign-" + str(uuid4()))
    await proposal(
        case, foreign_owner, "Foreign owner must not appear", scope="personal",
    )
    await bind_source(case, foreign_owner, project)
    await proposal(
        case, foreign_owner, "Foreign owner project must not appear",
        scope="project", project=project,
    )

    foreign_tenant = development_principals()[
        "dev-cross-tenant-operator"
    ].tenant_id
    assert foreign_tenant != principal.tenant_id
    foreign_source = await new_source(
        case, tenant=foreign_tenant, owner=principal.subject_id,
    )
    await proposal(
        case, foreign_source, "Foreign tenant must not appear", scope="personal",
    )

    reviewed_elsewhere = await new_source(case)
    await proposal(
        case, reviewed_elsewhere, "Different reviewer must not appear",
        scope="personal", reviewer="different-reviewer",
    )

    options = {}
    if binding == "personal":
        options["workspace_project_id"] = None
    elif binding == "project":
        options["workspace_project_id"] = project

    turn = await reserve(case, **options)
    expected = ["Current conversation accepted"]
    if binding == "project":
        expected.append("Matching project accepted")
    expected.append("Owner personal accepted")

    assert stored_memory_context(turn) == expected
    stored = await case["connection"].fetchrow(
        "SELECT * FROM conversation_turns WHERE turn_id=$1", turn["turn_id"],
    )
    assert stored_memory_context(dict(stored)) == expected


@pytest.mark.asyncio
async def test_http_review_acceptance_becomes_recallable(recall_case):
    case = recall_case
    pid = await proposal(
        case, target_source(case), "Approved through the actual review route",
        status="proposed",
    )
    assert await select_accepted_memories(
        case["connection"], case["principal"], case["cid"],
    ) == []

    path = f"/conversations/{case['cid']}/memory-proposals/{pid}/review"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            path,
            headers={"Authorization": "Bearer dev-requester"},
            json={"decision": "accepted", "memory_scope": "personal"},
        )
    assert response.status_code == 200, response.text

    turn = await reserve(case)
    assert stored_memory_context(turn) == [
        "Approved through the actual review route"
    ]


@pytest.mark.asyncio
async def test_retry_keeps_snapshot_after_new_acceptance(recall_case):
    case = recall_case
    source = await new_source(case)
    await proposal(case, source, "Original accepted memory", scope="personal")
    first = await reserve(case)

    await proposal(case, source, "Accepted later", scope="personal")
    current = await select_accepted_memories(
        case["connection"], case["principal"], case["cid"],
    )
    assert set(current) == {"Original accepted memory", "Accepted later"}

    retry = await reserve(case, memory_context=["Changed caller context"])
    assert retry["turn_id"] == first["turn_id"]
    assert retry["memory_context_sha256"] == first["memory_context_sha256"]
    assert stored_memory_context(retry) == ["Original accepted memory"]


@pytest.mark.asyncio
async def test_postgres_selection_limit_and_backend_priority(recall_case):
    case = recall_case
    source = await new_source(case)
    contents = {f"Accepted bounded memory {i}" for i in range(14)}
    for content in sorted(contents):
        await proposal(case, source, content, scope="personal")

    turn = await reserve(case, memory_context=["Legacy context beyond budget"])
    saved = stored_memory_context(turn)
    assert len(saved) == 12
    assert len(set(saved)) == 12
    assert set(saved).issubset(contents)
    assert "Legacy context beyond budget" not in saved

    assert saved == await select_accepted_memories(
        case["connection"], case["principal"], case["cid"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["retrieval", "queued_event"])
async def test_recall_reservation_failure_rolls_back(recall_case, monkeypatch, failure):
    case = recall_case
    source = await new_source(case)
    await proposal(case, source, "Recall rollback memory", scope="personal")
    queued_correlations = []

    if failure == "retrieval":
        actual_selector = coordinator.select_accepted_memories

        async def failing_selector(connection, principal, cid):
            selected = await actual_selector(connection, principal, cid)
            assert selected == ["Recall rollback memory"]
            await connection.execute("SELECT 1 / 0")
            return selected

        monkeypatch.setattr(
            coordinator, "select_accepted_memories", failing_selector,
        )

    async def audit(connection, **fields):
        await write_event(connection, **fields)
        if fields["event_type"] == "governance.conversation.turn.queued":
            queued_correlations.append(fields["correlation_id"])
            stored = await connection.fetchrow(
                "SELECT * FROM conversation_turns WHERE turn_id=$1",
                fields["correlation_id"],
            )
            assert stored_memory_context(dict(stored)) == ["Recall rollback memory"]
            assert await connection.fetchval(
                "SELECT count(*) FROM conversation_workspace_bindings "
                "WHERE tenant_id=$1 AND conversation_id=$2",
                case["principal"].tenant_id, case["cid"],
            ) == 1
            raise RuntimeError("injected recall queued-event failure")

    error = asyncpg.DivisionByZeroError if failure == "retrieval" else RuntimeError
    with pytest.raises(error):
        await coordinator.reserve_turn(
            case["pool"], case["principal"], case["cid"], case["user"],
            write_event=audit, workspace_project_id=uuid4(),
        )

    connection = case["connection"]
    for table in ("conversation_turns", "conversation_workspace_bindings"):
        assert await connection.fetchval(
            "SELECT count(*) FROM " + table +
            " WHERE tenant_id=$1 AND conversation_id=$2",
            case["principal"].tenant_id, case["cid"],
        ) == 0

    assert await connection.fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.workspace.bound'",
        case["cid"],
    ) == 0
    for correlation in queued_correlations:
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1", correlation,
        ) == 0

    assert await connection.fetchval(
        "SELECT count(*) FROM conversation_memory_proposals "
        "WHERE tenant_id=$1 AND conversation_id=$2 AND status='accepted'",
        source[0], source[1],
    ) == 1
    assert len(queued_correlations) == (1 if failure == "queued_event" else 0)
