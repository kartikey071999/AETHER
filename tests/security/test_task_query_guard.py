"""Static guard: agents read tasks through their work queue, never by querying TaskRecord.

``aether.agents.work_queue`` decides deterministically which tasks an agent sees (its own open
tasks by effective priority; finished tasks only on request; cross-agent reads denied and
recorded, unless a relation grants the read). Building a ``select``/``join``/``update``/``delete``
over ``TaskRecord``, or a point lookup such as ``session.get(TaskRecord, id)``, anywhere else
under ``agents/`` would bypass those rules. The allowlist is for ORCHESTR's system-wide scheduling
store and the read models that by design see every agent's tasks; each entry must still need it.
"""

import ast
from pathlib import Path

AGENTS_ROOT = Path(__file__).resolve().parents[2] / "src" / "aether" / "agents"
QUERY_BUILDERS = frozenset({"select", "select_from", "join", "outerjoin", "update", "delete"})
TYPE_CHECKS = frozenset({"isinstance", "issubclass", "cast"})
GUARDED_MODEL = "TaskRecord"
QUEUE_MODULE = "work_queue.py"
SYSTEM_WIDE_TASK_QUERIES = {
    # ORCHESTR is the coordinator: it plans, schedules and settles every agent's tasks.
    "orchestr/store.py": "ORCHESTR's planning and scheduling store spans all agents",
    "orchestr/pressure.py": "ORCHESTR pauses/resumes running work under resource pressure",
    # LEDGER is the system of record: its operational read model reports on all tasks.
    "ledger/queries.py": "LEDGER's operational snapshot and progress read model",
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _mentions_model(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == GUARDED_MODEL for child in ast.walk(node)
    )


def task_queries(code: str) -> list[int]:
    """Line numbers of query builders that take ``TaskRecord`` (or its columns) as arguments."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.Call) or _call_name(node) not in QUERY_BUILDERS:
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(_mentions_model(argument) for argument in arguments):
            lines.append(node.lineno)
    return lines


def task_lookups(code: str) -> list[int]:
    """Line numbers of calls that pass the bare ``TaskRecord`` class (``session.get`` and kin)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.Call) or _call_name(node) in TYPE_CHECKS:
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(isinstance(it, ast.Name) and it.id == GUARDED_MODEL for it in arguments):
            lines.append(node.lineno)
    return lines


def _task_reads(code: str) -> list[int]:
    return sorted({*task_queries(code), *task_lookups(code)})


def _agent_files() -> dict[str, str]:
    return {
        path.relative_to(AGENTS_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(AGENTS_ROOT.rglob("*.py"))
    }


def test_detector_catches_task_list_queries() -> None:
    assert task_queries("select(TaskRecord).where(TaskRecord.state == 'ready')") == [1]
    assert task_queries("select(TaskRecord.id, TaskRecord.state)") == [1]
    assert task_queries("select(func.count()).select_from(TaskRecord)") == [1]
    assert task_queries("select(Other).join(TaskRecord, TaskRecord.id == Other.task_id)") == [1]
    assert task_queries("update(TaskRecord).values(state='x')") == [1]
    assert task_queries("sa.delete(TaskRecord)") == [1]
    assert task_queries("select(\n    TaskRecord\n)") == [1]


def test_detector_catches_point_lookups() -> None:
    assert task_lookups("await session.get(TaskRecord, task_id)") == [1]
    assert task_lookups("await session.get(\n    TaskRecord, task_id, populate_existing=True)") == [
        1
    ]
    assert task_lookups("await existing_id(session, TaskRecord, task_id)") == [1]
    assert task_lookups("await session.get(model=TaskRecord, ident=task_id)") == [1]


def test_detector_allows_other_tables_and_type_uses() -> None:
    assert task_queries("select(MessageRecord).where(MessageRecord.task_id == task_id)") == []
    assert task_queries("def execute_task(self, task: TaskRecord): ...") == []
    assert task_lookups("await session.get(ObjectiveRecord, objective_id)") == []
    assert task_lookups("def execute_task(self, task: TaskRecord): ...") == []
    assert task_lookups("isinstance(value, TaskRecord)") == []
    assert task_lookups("await queue.task_detail(task_id, message=message)") == []


def test_agents_do_not_query_task_lists_directly() -> None:
    offenders = {
        name: lines
        for name, code in _agent_files().items()
        if name != QUEUE_MODULE
        and name not in SYSTEM_WIDE_TASK_QUERIES
        and (lines := task_queries(code))
    }
    assert offenders == {}, (
        "agents must read task lists via their work queue (BaseAgent.work_queue / "
        f"aether.agents.work_queue), not by querying TaskRecord: {offenders}"
    )


def test_agents_do_not_look_up_tasks_directly() -> None:
    offenders = {
        name: lines
        for name, code in _agent_files().items()
        if name != QUEUE_MODULE
        and name not in SYSTEM_WIDE_TASK_QUERIES
        and (lines := task_lookups(code))
    }
    assert offenders == {}, (
        "agents must read a single task via the work queue (task_detail with a message or "
        "relation, task_record, task_ref), not session.get(TaskRecord, ...): "
        f"{offenders}"
    )


def test_work_queue_module_is_where_task_lists_are_built() -> None:
    assert task_queries(_agent_files()[QUEUE_MODULE])


def test_allowlist_is_not_stale() -> None:
    files = _agent_files()
    stale = sorted(
        name for name in SYSTEM_WIDE_TASK_QUERIES if not _task_reads(files.get(name, ""))
    )
    assert stale == [], f"remove allowlist entries that no longer read TaskRecord: {stale}"
