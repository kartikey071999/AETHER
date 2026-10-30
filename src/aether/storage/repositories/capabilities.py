"""Capability registry with an enforced lifecycle."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import CapabilityRecord, CapabilityStatus, EventCategory, utcnow
from aether.storage.repositories._events import add_event

# A capability only becomes verified after LEDGER testing; every other path back to
# availability goes through installed -> tested -> verified again.
_ALLOWED_CAPABILITY_TRANSITIONS: dict[CapabilityStatus, frozenset[CapabilityStatus]] = {
    CapabilityStatus.DISCOVERED: frozenset(
        {CapabilityStatus.PROPOSED, CapabilityStatus.DEPRECATED}
    ),
    CapabilityStatus.PROPOSED: frozenset({CapabilityStatus.APPROVED, CapabilityStatus.DEPRECATED}),
    CapabilityStatus.APPROVED: frozenset(
        {CapabilityStatus.INSTALLED, CapabilityStatus.DISABLED, CapabilityStatus.DEPRECATED}
    ),
    CapabilityStatus.INSTALLED: frozenset(
        {CapabilityStatus.TESTED, CapabilityStatus.DISABLED, CapabilityStatus.DEPRECATED}
    ),
    CapabilityStatus.TESTED: frozenset(
        {
            CapabilityStatus.VERIFIED,
            CapabilityStatus.INSTALLED,
            CapabilityStatus.DISABLED,
            CapabilityStatus.DEPRECATED,
        }
    ),
    CapabilityStatus.VERIFIED: frozenset(
        {CapabilityStatus.INSTALLED, CapabilityStatus.DISABLED, CapabilityStatus.DEPRECATED}
    ),
    CapabilityStatus.DISABLED: frozenset({CapabilityStatus.INSTALLED, CapabilityStatus.DEPRECATED}),
    CapabilityStatus.DEPRECATED: frozenset(),
}

_VERIFICATION_STATUS_BY_LIFECYCLE: dict[CapabilityStatus, str] = {
    CapabilityStatus.INSTALLED: "unverified",
    CapabilityStatus.TESTED: "tested",
    CapabilityStatus.VERIFIED: "verified",
}


class InvalidCapabilityTransition(ValueError):
    pass


def allowed_capability_transitions(status: CapabilityStatus) -> frozenset[CapabilityStatus]:
    return _ALLOWED_CAPABILITY_TRANSITIONS[CapabilityStatus(status)]


class CapabilityRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def register(
        self,
        *,
        name: str,
        actor: str,
        description: str = "",
        provider: str | None = None,
        version: str | None = None,
        entrypoint: str | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        required_permissions: list[str] | None = None,
        dependencies: list[str] | None = None,
        resource_estimates: dict[str, Any] | None = None,
        known_limitations: list[str] | None = None,
        requirement_id: UUID | None = None,
        objective_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Record a newly discovered capability; it always starts as ``discovered``."""
        async with self._sessions() as session, session.begin():
            capability = CapabilityRecord(
                name=name,
                description=description,
                lifecycle_status=CapabilityStatus.DISCOVERED,
                provider=provider,
                version=version,
                entrypoint=entrypoint,
                input_schema=input_schema or {},
                output_schema=output_schema or {},
                required_permissions=required_permissions or [],
                dependencies=dependencies or [],
                resource_estimates=resource_estimates or {},
                verification_status="unverified",
                known_limitations=known_limitations or [],
                requirement_id=requirement_id,
                objective_id=objective_id,
            )
            session.add(capability)
            await session.flush()
            add_event(
                session,
                event_type="capability.discovered",
                aggregate_type="capability",
                aggregate_id=capability.id,
                actor=actor,
                payload={"name": name, "provider": provider, "version": version},
                category=EventCategory.CAPABILITY,
                correlation_id=correlation_id,
                objective_id=objective_id,
                idempotency_key=f"capability.discovered:{capability.id}",
            )
            return capability.id

    async def transition(
        self,
        capability_id: UUID,
        new_status: CapabilityStatus,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        new_status = CapabilityStatus(new_status)
        async with self._sessions() as session, session.begin():
            statement = select(CapabilityRecord).where(CapabilityRecord.id == capability_id)
            capability = await session.scalar(statement.with_for_update())
            if capability is None:
                raise LookupError(f"Capability {capability_id} does not exist")
            old_status = CapabilityStatus(capability.lifecycle_status)
            if new_status not in _ALLOWED_CAPABILITY_TRANSITIONS[old_status]:
                raise InvalidCapabilityTransition(
                    f"Cannot transition capability from {old_status} to {new_status}"
                )
            capability.lifecycle_status = new_status
            capability.updated_at = utcnow()
            if new_status in _VERIFICATION_STATUS_BY_LIFECYCLE:
                capability.verification_status = _VERIFICATION_STATUS_BY_LIFECYCLE[new_status]
            add_event(
                session,
                event_type="capability.transitioned",
                aggregate_type="capability",
                aggregate_id=capability.id,
                actor=actor,
                payload={
                    "from": old_status,
                    "to": new_status,
                    "details": details or {},
                },
                category=EventCategory.CAPABILITY,
                correlation_id=correlation_id,
                objective_id=capability.objective_id,
            )

    async def get_by_name(self, name: str) -> CapabilityRecord | None:
        statement = select(CapabilityRecord).where(CapabilityRecord.name == name)
        async with self._sessions() as session:
            return await session.scalar(statement)

    async def list_by_status(self, status: CapabilityStatus) -> list[CapabilityRecord]:
        condition = CapabilityRecord.lifecycle_status == CapabilityStatus(status)
        async with self._sessions() as session:
            records = await session.scalars(select(CapabilityRecord).where(condition))
            return list(records)
