"""Scenario 10: concurrent dependency additions must never create a cycle.

Cycle detection reads the dependency graph and then inserts an edge, so two writers adding
opposite (or cycle-closing) edges at the same time could each see an acyclic graph and both
commit. ``TaskDependencyRepository.add`` serializes graph changes before reading, so exactly one
of the racing edges is accepted on both SQLite and PostgreSQL.
"""

import asyncio
from uuid import UUID

import pytest
from reliability_support import make_task, postgres_databases
from sqlalchemy import select

from aether.storage.models import TaskDependencyRecord
from aether.storage.repositories import ObjectiveRepository
from aether.storage.repositories import dependencies as dependency_module
from aether.storage.repositories.dependencies import (
    DependencyCycleError,
    TaskDependencyRepository,
)


def _interleave_after_cycle_check(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
    """Make every racing writer finish its cycle check before any of them inserts.

    The wait is bounded: when the repository serializes writers correctly, later writers are
    still blocked on the lock, so the first one proceeds alone after the timeout.
    """
    barrier = asyncio.Barrier(parties)
    original = dependency_module._reaches

    async def reaches_then_wait(session, *, start: UUID, target: UUID) -> bool:
        found = await original(session, start=start, target=target)
        try:
            await asyncio.wait_for(barrier.wait(), timeout=0.5)
        except (TimeoutError, asyncio.BrokenBarrierError):
            pass
        return found

    monkeypatch.setattr(dependency_module, "_reaches", reaches_then_wait)


async def _race(*attempts) -> list[bool | BaseException]:
    return list(await asyncio.gather(*attempts, return_exceptions=True))


async def _edges(sessions) -> set[tuple[UUID, UUID]]:
    async with sessions() as session:
        rows = await session.execute(
            select(TaskDependencyRecord.task_id, TaskDependencyRecord.depends_on_task_id)
        )
        return {(task, dependency) for task, dependency in rows.all()}


def _has_cycle(edges: set[tuple[UUID, UUID]]) -> bool:
    graph: dict[UUID, set[UUID]] = {}
    for task, dependency in edges:
        graph.setdefault(task, set()).add(dependency)
    visiting: set[UUID] = set()
    done: set[UUID] = set()

    def visit(node: UUID) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        cyclic = any(visit(child) for child in graph.get(node, ()))
        visiting.discard(node)
        done.add(node)
        return cyclic

    return any(visit(node) for node in list(graph))


def _assert_one_winner(results: list[bool | BaseException]) -> None:
    assert sum(result is True for result in results) == 1, results
    losers = [result for result in results if result is not True]
    assert all(isinstance(result, DependencyCycleError) for result in losers), results


async def test_opposite_edges_added_concurrently_cannot_both_succeed(
    database, peer_database, objective_id, monkeypatch
) -> None:
    a = await make_task(database.sessions, objective_id, title="a")
    b = await make_task(database.sessions, objective_id, title="b")
    _interleave_after_cycle_check(monkeypatch, 2)

    results = await _race(
        TaskDependencyRepository(database.sessions).add(a, b, actor="ORCHESTR"),
        TaskDependencyRepository(peer_database.sessions).add(b, a, actor="ORCHESTR"),
    )

    _assert_one_winner(results)
    edges = await _edges(database.sessions)
    assert len(edges) == 1 and not _has_cycle(edges)


async def test_cycle_closing_edges_over_disjoint_tasks_cannot_both_succeed(
    database, peer_database, objective_id, monkeypatch
) -> None:
    """a->b and c->d exist; b->c and d->a each look safe alone but together close a cycle."""
    a, b, c, d = [await make_task(database.sessions, objective_id, title=name) for name in "abcd"]
    repository = TaskDependencyRepository(database.sessions)
    assert await repository.add(a, b, actor="ORCHESTR")
    assert await repository.add(c, d, actor="ORCHESTR")
    _interleave_after_cycle_check(monkeypatch, 2)

    results = await _race(
        TaskDependencyRepository(database.sessions).add(b, c, actor="ORCHESTR"),
        TaskDependencyRepository(peer_database.sessions).add(d, a, actor="ORCHESTR"),
    )

    _assert_one_winner(results)
    edges = await _edges(database.sessions)
    assert len(edges) == 3 and not _has_cycle(edges)


async def test_racing_duplicate_edges_are_added_once(database, peer_database, objective_id) -> None:
    a = await make_task(database.sessions, objective_id, title="a")
    b = await make_task(database.sessions, objective_id, title="b")

    results = await _race(
        *(
            TaskDependencyRepository(db.sessions).add(a, b, actor="ORCHESTR")
            for db in (database, peer_database, database)
        )
    )

    assert sorted(results) == [False, False, True], results
    assert await _edges(database.sessions) == {(a, b)}


@pytest.mark.postgres
async def test_postgres_opposite_and_cycle_closing_edges_race_safely(monkeypatch) -> None:
    async with postgres_databases(2) as (first, second):
        objective_id = await ObjectiveRepository(first.sessions).create(title="race")
        a, b, c, d = [await make_task(first.sessions, objective_id, title=n) for n in "abcd"]
        _interleave_after_cycle_check(monkeypatch, 2)

        opposite = await _race(
            TaskDependencyRepository(first.sessions).add(a, b, actor="ORCHESTR"),
            TaskDependencyRepository(second.sessions).add(b, a, actor="ORCHESTR"),
        )
        _assert_one_winner(opposite)

        winner = (a, b) if opposite[0] is True else (b, a)
        assert await TaskDependencyRepository(first.sessions).add(c, d, actor="ORCHESTR")
        # Close a 4-cycle over rows the two writers do not share: (b->c, d->a) or (a->c, d->b).
        head, tail = winner
        closing = await _race(
            TaskDependencyRepository(first.sessions).add(tail, c, actor="ORCHESTR"),
            TaskDependencyRepository(second.sessions).add(d, head, actor="ORCHESTR"),
        )
        _assert_one_winner(closing)
        edges = await _edges(first.sessions)
        assert len(edges) == 3 and not _has_cycle(edges)
