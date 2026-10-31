from datetime import UTC, datetime
from uuid import uuid4

from aether.storage.database import Database
from aether.storage.models import AuthorizationRequestRecord, SecurityDecisionRecord
from aether.storage.repositories import AuthorizationRequestRepository
from aether.storage.repositories.security import OperationAuthorizationRequests

INPUT_HASH = "a" * 64


async def test_create_stores_input_hash(database: Database) -> None:
    repository = AuthorizationRequestRepository(database.sessions)

    request_id = await repository.create(
        question="Run it?", actor="ORCHESTR", input_hash=INPUT_HASH
    )
    other_id = await repository.create(question="Run something else?", actor="ORCHESTR")

    async with database.sessions() as session:
        request = await session.get(AuthorizationRequestRecord, request_id)
        other = await session.get(AuthorizationRequestRecord, other_id)
    assert request is not None
    assert request.input_hash == INPUT_HASH
    assert other is not None
    assert other.input_hash is None


async def test_stage_pending_stores_input_hash(database: Database) -> None:
    decision_id = uuid4()
    async with database.sessions() as session, session.begin():
        session.add(
            SecurityDecisionRecord(
                id=decision_id,
                decision="approved",
                operation="op",
                scope={},
                evidence=[],
                rationale="test",
                input_hash=INPUT_HASH,
            )
        )
        record = await OperationAuthorizationRequests.stage_pending(
            session,
            question="Authorize?",
            security_decision_id=decision_id,
            requested_at=datetime.now(UTC),
            expires_at=None,
            input_hash=INPUT_HASH,
        )
        assert record.input_hash == INPUT_HASH
