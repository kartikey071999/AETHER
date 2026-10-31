"""Capability registry with an enforced lifecycle."""

import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    CapabilityRecord,
    CapabilityStatus,
    EventCategory,
    EventRecord,
    utcnow,
)
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

    async def record_discovery(
        self,
        *,
        name: str,
        actor: str,
        finding_id: UUID,
        objective_id: UUID | None,
        evidence_ids: list[UUID],
        description: str = "",
        dependencies: list[str] | None = None,
        resource_estimates: dict[str, Any] | None = None,
        known_limitations: list[str] | None = None,
        required_permissions: list[str] | None = None,
        requirement_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> CapabilityRecord:
        """Consolidate an approach inside SCOUT's serialized research transaction.

        Evidence snapshots remain auditable even after review freezes the registry
        metadata. The existing unique name constraint covers new objective-scoped
        identities without changing installation/verification names.
        """
        normalized = " ".join(name.split()).casefold()
        if not normalized:
            raise ValueError("A discovered approach must have a nonblank name")
        identity = uuid5(objective_id or NAMESPACE_URL, normalized)
        registry_name = f"{normalized[:150]} [{identity}]"
        async with self._sessions() as session, session.begin():
            records = await session.scalars(
                select(CapabilityRecord)
                .where(CapabilityRecord.objective_id == objective_id)
                .order_by(CapabilityRecord.created_at, CapabilityRecord.id)
                .with_for_update()
            )
            matches = [
                record
                for record in records
                if record.name == registry_name
                or " ".join(re.sub(r" \[[0-9a-fA-F-]{36}\]$", "", record.name).split()).casefold()
                == normalized
            ]
            # Reuse legacy finding-scoped rows, preferring already reviewed work.
            capability = next(
                (row for row in matches if row.lifecycle_status == CapabilityStatus.VERIFIED),
                next(
                    (row for row in matches if row.lifecycle_status != CapabilityStatus.DISCOVERED),
                    matches[0] if matches else None,
                ),
            )
            if capability is None:
                capability = CapabilityRecord(
                    name=registry_name,
                    objective_id=objective_id,
                    lifecycle_status=CapabilityStatus.DISCOVERED,
                    verification_status="unverified",
                )
                session.add(capability)
                await session.flush()
                add_event(
                    session,
                    event_type="capability.discovered",
                    aggregate_type="capability",
                    aggregate_id=capability.id,
                    actor=actor,
                    payload={"name": registry_name},
                    category=EventCategory.CAPABILITY,
                    correlation_id=correlation_id,
                    objective_id=objective_id,
                    idempotency_key=f"capability.discovered:{capability.id}",
                )
            key = f"capability.evidence_added:{capability.id}:{finding_id}"
            if await session.scalar(
                select(EventRecord.id).where(EventRecord.idempotency_key == key)
            ):
                if (
                    capability.lifecycle_status == CapabilityStatus.DISCOVERED
                    and capability.requirement_id is None
                    and requirement_id is not None
                ):
                    capability.requirement_id = requirement_id
                    capability.updated_at = utcnow()
                    await session.flush()
                return capability
            if capability.lifecycle_status == CapabilityStatus.DISCOVERED:
                capability.description = description or capability.description
                capability.dependencies = list(
                    dict.fromkeys([*capability.dependencies, *(dependencies or [])])
                )
                capability.known_limitations = list(
                    dict.fromkeys([*capability.known_limitations, *(known_limitations or [])])
                )
                capability.required_permissions = list(
                    dict.fromkeys([*capability.required_permissions, *(required_permissions or [])])
                )
                capability.resource_estimates = {
                    **capability.resource_estimates,
                    **(resource_estimates or {}),
                }
                capability.requirement_id = capability.requirement_id or requirement_id
                capability.updated_at = utcnow()
            add_event(
                session,
                event_type="capability.evidence_added",
                aggregate_type="capability",
                aggregate_id=capability.id,
                actor=actor,
                payload={
                    "approach_name": name,
                    "finding_id": finding_id,
                    "evidence_ids": evidence_ids,
                    "description": description,
                    "dependencies": dependencies or [],
                    "resource_estimates": resource_estimates or {},
                    "known_limitations": known_limitations or [],
                    "requirement_id": requirement_id,
                },
                category=EventCategory.CAPABILITY,
                correlation_id=correlation_id,
                objective_id=objective_id,
                idempotency_key=key,
            )
            await session.flush()
            return capability

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
                    "from_status": old_status,
                    "to_status": new_status,
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
