"""SafeSubprocess: allowlisted argv execution with confinement, timeouts, and audit records."""

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import EventRecorder
from aether.security._audit import stage_event
from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import SecurityPolicy
from aether.security.untrusted import UntrustedContent
from aether.storage.models import RunStatus
from aether.storage.repositories.execution import ExecutionRunRepository
from aether.tools.adapters._base import ExecutionFailed, admit
from aether.tools.registry import OperationSpec

SOURCE = "aether.tools.subprocess"
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE_URL", "CREDENTIAL")
_CHUNK = 64 * 1024
_READER_GRACE_SECONDS = 5.0
_KILL_WAIT_SECONDS = 5.0


def scrub_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``environment`` without variables whose names suggest secrets."""
    return {
        name: value
        for name, value in environment.items()
        if not any(marker in name.upper() for marker in SECRET_ENV_MARKERS)
    }


def kill_process_tree(pid: int) -> list[int]:
    """Kill ``pid`` and all descendants (works on Windows, where children are not killed
    with their parent). Returns the PIDs that were signalled."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    try:
        descendants = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    killed: list[int] = []
    # Kill the parent first so it cannot spawn replacements, then every recorded descendant.
    for process in (parent, *descendants):
        try:
            process.kill()
            killed.append(process.pid)
        except psutil.NoSuchProcess:
            continue
    # The direct child is reaped by asyncio; only wait for the descendants here.
    psutil.wait_procs(descendants, timeout=_KILL_WAIT_SECONDS)
    return killed


class SubprocessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    run_id: UUID
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    stdout: UntrustedContent
    stderr: UntrustedContent
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def _pump(stream: asyncio.StreamReader | None, path: Path, cap: int) -> tuple[int, bool]:
    written = 0
    truncated = False
    with path.open("wb") as handle:
        if stream is None:
            return 0, False
        while chunk := await stream.read(_CHUNK):
            room = cap - written
            if room > 0:
                handle.write(chunk[:room])
                written += min(room, len(chunk))
            if len(chunk) > max(room, 0):
                truncated = True
    return written, truncated


class SafeSubprocess:
    """Runs an argv list (never a shell) only after a matching single-use Authorization."""

    kinds = frozenset({OperationKind.SUBPROCESS})

    def __init__(
        self,
        permissions: PermissionService,
        sessions: async_sessionmaker[AsyncSession],
        recorder: EventRecorder,
        *,
        output_dir: str | Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._permissions = permissions
        self._sessions = sessions
        self._recorder = recorder
        self._output_dir = Path(output_dir) if output_dir else None
        self._clock = clock

    @property
    def _policy(self) -> SecurityPolicy:
        return self._permissions.policy

    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec:
        if not isinstance(payload, SubprocessInput):
            raise TypeError("SafeSubprocess expects SubprocessInput")
        limit = self._policy.subprocess_timeout_seconds
        timeout = min(payload.timeout_seconds or limit, limit)
        return OperationSpec(
            target=payload.argv[0],
            arguments={"argv": list(payload.argv), "timeout_seconds": timeout},
            cwd=payload.cwd or str(self._policy.workspace_dir),
        )

    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> SubprocessResult:
        operation, grant = await admit(
            self._permissions, self.kinds, operation, authorization, correlation_id=correlation_id
        )
        policy = self._policy

        async def refuse(detail: str) -> Exception:
            return await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                detail,
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            )

        argv = operation.arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
            or argv[0] != operation.target
        ):
            raise await refuse("argv must be a non-empty list of strings starting with the target")
        timeout = operation.arguments.get("timeout_seconds", policy.subprocess_timeout_seconds)
        if not isinstance(timeout, int | float) or not 0 < timeout <= (
            policy.subprocess_timeout_seconds
        ):
            raise await refuse("timeout is outside the policy limit")
        executable = policy.resolve_executable(operation.target)
        cwd = policy.confine_path(operation.cwd or policy.workspace_dir)
        if executable is None:
            raise await refuse("executable is not allowlisted")
        if cwd is None or not cwd.is_dir():
            raise await refuse("working directory is not an existing allowed directory")

        run_id = uuid4()
        output_root = policy.confine_path(self._output_dir or policy.artifact_dir / "executions")
        if output_root is None:
            raise await refuse("execution output directory is outside the allowed roots")
        run_dir = output_root / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = run_dir / "stdout.log", run_dir / "stderr.log"

        await self._record_start(operation, grant, run_id, stdout_path, stderr_path, correlation_id)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *argv[1:],
                cwd=str(cwd),
                env=scrub_environment(os.environ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            await self._record_finish(
                operation,
                run_id,
                RunStatus.FAILED,
                None,
                time.monotonic() - started,
                {"error": f"{type(error).__name__}: {error}"},
                correlation_id,
            )
            raise ExecutionFailed(f"could not start {operation.describe()}") from error

        cap = policy.max_output_bytes
        readers = [
            asyncio.ensure_future(_pump(process.stdout, stdout_path, cap)),
            asyncio.ensure_future(_pump(process.stderr, stderr_path, cap)),
        ]
        timed_out = False
        killed: list[int] = []
        try:
            try:
                exit_code: int | None = await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                killed = await asyncio.to_thread(kill_process_tree, process.pid)
                exit_code = await process.wait()
            done, pending = await asyncio.wait(readers, timeout=_READER_GRACE_SECONDS)
            for reader in pending:
                reader.cancel()
        except (asyncio.CancelledError, Exception) as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            await asyncio.shield(asyncio.to_thread(kill_process_tree, process.pid))
            for reader in readers:
                reader.cancel()
            await asyncio.shield(
                self._record_finish(
                    operation,
                    run_id,
                    RunStatus.CANCELLED if cancelled else RunStatus.FAILED,
                    None,
                    time.monotonic() - started,
                    {"cancelled": cancelled, "error": None if cancelled else type(error).__name__},
                    correlation_id,
                )
            )
            raise
        duration = time.monotonic() - started
        stdout_info = readers[0].result() if readers[0] in done else (0, True)
        stderr_info = readers[1].result() if readers[1] in done else (0, True)
        usage = {
            "duration_seconds": round(duration, 3),
            "timed_out": timed_out,
            "killed_processes": len(killed),
            "stdout_bytes": stdout_info[0],
            "stderr_bytes": stderr_info[0],
            "stdout_truncated": stdout_info[1],
            "stderr_truncated": stderr_info[1],
        }
        status = RunStatus.SUCCEEDED if exit_code == 0 and not timed_out else RunStatus.FAILED
        await self._record_finish(
            operation, run_id, status, exit_code, duration, usage, correlation_id
        )
        source = f"subprocess:{run_id}"
        return SubprocessResult(
            run_id=run_id,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout=_read_capped(stdout_path, f"{source}:stdout"),
            stderr=_read_capped(stderr_path, f"{source}:stderr"),
            stdout_truncated=stdout_info[1],
            stderr_truncated=stderr_info[1],
        )

    async def _record_start(
        self,
        operation: Operation,
        grant: Authorization,
        run_id: UUID,
        stdout_path: Path,
        stderr_path: Path,
        correlation_id: UUID | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await ExecutionRunRepository.stage_started(
                session,
                run_id=run_id,
                agent=operation.requested_by.value,
                operation=operation.describe(),
                started_at=self._clock(),
                task_id=operation.task_id,
                security_decision_id=grant.decision_id,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )
            stage_event(
                session,
                self._recorder,
                "execution.started",
                source=SOURCE,
                actor=operation.requested_by.value,
                aggregate_type="execution_run",
                aggregate_id=run_id,
                payload={"execution_id": run_id, "operation": operation.describe()},
                objective_id=operation.objective_id,
                task_id=operation.task_id,
                correlation_id=correlation_id,
            )

    async def _record_finish(
        self,
        operation: Operation,
        run_id: UUID,
        status: RunStatus,
        exit_code: int | None,
        duration: float,
        usage: dict[str, Any],
        correlation_id: UUID | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await ExecutionRunRepository.stage_finished(
                session,
                run_id,
                status=status,
                finished_at=self._clock(),
                exit_code=exit_code,
                resource_usage=usage,
            )
            stage_event(
                session,
                self._recorder,
                "execution.finished",
                source=SOURCE,
                actor=operation.requested_by.value,
                aggregate_type="execution_run",
                aggregate_id=run_id,
                payload={
                    "execution_id": run_id,
                    "success": status is RunStatus.SUCCEEDED,
                    "duration_seconds": max(duration, 0.0),
                    "exit_code": exit_code,
                    "details": {"status": status.value, **usage},
                },
                objective_id=operation.objective_id,
                task_id=operation.task_id,
                correlation_id=correlation_id,
            )


def _read_capped(path: Path, source: str) -> UntrustedContent:
    data = path.read_bytes() if path.exists() else b""
    return UntrustedContent(source=source, content=data.decode("utf-8", errors="replace"))
