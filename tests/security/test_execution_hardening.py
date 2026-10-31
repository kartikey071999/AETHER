import asyncio
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpcore
import httpx
import psutil
import pytest
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.observability.events import build_event
from aether.security import (
    BlockReason,
    InvalidSecurityDecision,
    OperationBlocked,
    PermissionService,
)
from aether.security.permissions import parse_restrictions
from aether.storage.models import ExecutionRunRecord, RunStatus
from aether.storage.repositories.security import SecurityDecisionRepository
from aether.tools.adapters import SafeFiles, SafeHttp, SafeSubprocess
from aether.tools.adapters._base import ExecutionFailed
from aether.tools.adapters._containment import ProcessContainment

HOST = "api.example.com"
PUBLIC_IP = "93.184.216.34"
TREE = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path("parent.pid").write_text(str(__import__("os").getpid()))
pathlib.Path("grandchild.pid").write_text(str(child.pid))
"""


def _addresses(address: str, port: int) -> list:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def _is_gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def _assert_tree_gone(roots: dict[str, Path]) -> None:
    pids = [
        int((roots["workspace"] / name).read_text()) for name in ("parent.pid", "grandchild.pid")
    ]
    async with asyncio.timeout(3):
        while not all(_is_gone(pid) for pid in pids):
            await asyncio.sleep(0.02)


async def _http_operation(registry, approve, permissions, **kwargs):
    args = {"url": f"https://{HOST}/data", **kwargs}
    operation = registry.build_operation(AgentId.SCOUT, "http_request", args)
    await approve(operation)
    return operation, await permissions.authorize(operation)


async def test_grandchild_killed_after_normal_parent_exit(
    registry, approve, roots, events_of
) -> None:
    args = {"argv": [sys.executable, "-c", TREE], "timeout_seconds": 2}
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    result = await registry.invoke(AgentId.FORGE, "run_process", args)
    assert result.succeeded
    assert result.duration_seconds < 2
    await _assert_tree_gone(roots)
    assert (await events_of("execution.finished"))[0].payload["success"]


async def test_grandchild_killed_on_timeout(registry, approve, roots) -> None:
    args = {
        "argv": [sys.executable, "-c", TREE + "\ntime.sleep(60)"],
        "timeout_seconds": 1,
    }
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    result = await registry.invoke(AgentId.FORGE, "run_process", args)
    assert result.timed_out
    await _assert_tree_gone(roots)


async def test_grandchild_killed_on_cancellation(registry, approve, roots, database) -> None:
    args = {"argv": [sys.executable, "-c", TREE + "\ntime.sleep(60)"]}
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    running = asyncio.create_task(registry.invoke(AgentId.FORGE, "run_process", args))
    try:
        async with asyncio.timeout(3):
            while not (roots["workspace"] / "grandchild.pid").exists():
                await asyncio.sleep(0.02)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await _assert_tree_gone(roots)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert run.status == RunStatus.CANCELLED
    assert run.finished_at is not None


async def test_failed_job_attachment_never_runs_child(
    registry, approve, monkeypatch, roots, database
) -> None:
    def fail_attach(self, pid):
        if sys.platform == "win32":
            time.sleep(0.1)
        raise OSError("simulated containment failure")

    monkeypatch.setattr(ProcessContainment, "attach", fail_attach)
    # A suspended Windows child must not execute this script before successful attachment.
    args = {
        "argv": [
            sys.executable,
            "-c",
            (
                "import pathlib, sys, time; "
                "time.sleep(0 if sys.platform == 'win32' else 0.2); "
                "pathlib.Path('ran.txt').write_text('ran')"
            ),
        ]
    }
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    with pytest.raises(ExecutionFailed):
        await registry.invoke(AgentId.FORGE, "run_process", args)
    assert not (roots["workspace"] / "ran.txt").exists()
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionRunRecord))).one().status == RunStatus.FAILED


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_private_dns_resolution_blocked(
    address, registry, approve, permissions, database, recorder, events_of
) -> None:
    requests = []
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(address, port),
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    with pytest.raises(OperationBlocked) as raised:
        await adapter.execute(operation, grant)
    assert raised.value.reason == BlockReason.POLICY_VIOLATION
    assert requests == []
    assert (await events_of("security.operation_blocked"))[0].payload["input_hash"] == (
        operation.input_hash
    )
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert run.status == RunStatus.FAILED
    assert run.security_decision_id == grant.decision_id


async def test_mixed_public_private_dns_answers_fail_closed(
    registry, approve, permissions, database, recorder
) -> None:
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port) + _addresses("10.0.0.1", port),
        transport=httpx.MockTransport(lambda request: pytest.fail("request must not be sent")),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    with pytest.raises(OperationBlocked):
        await adapter.execute(operation, grant)


async def test_dns_rechecked_and_private_redirect_blocked(
    registry, approve, permissions, database, recorder
) -> None:
    resolutions, requests = [], []

    def resolve(host, port):
        resolutions.append(host)
        return _addresses(PUBLIC_IP if len(resolutions) == 1 else "10.0.0.1", port)

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/private"})

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=resolve,
        transport=httpx.MockTransport(handler),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    with pytest.raises(OperationBlocked):
        await adapter.execute(operation, grant)
    assert resolutions == [HOST, HOST]
    assert len(requests) == 1
    assert requests[0].url.host == PUBLIC_IP


async def test_pinned_ip_original_host_sni_and_http_run(
    registry, approve, permissions, database, recorder, events_of
) -> None:
    calls, requests = [], []

    def resolve(host, port):
        calls.append((host, port))
        return _addresses(PUBLIC_IP, port)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="ok")

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=resolve,
        transport=httpx.MockTransport(handler),
    )
    url = f"https://{HOST}:8443/data"
    operation, grant = await _http_operation(
        registry, approve, permissions, url=url, headers={"Host": "unapproved.example.net"}
    )
    result = await adapter.execute(operation, grant)
    assert calls == [(HOST, 8443)]
    assert requests[0].url.host == PUBLIC_IP
    assert requests[0].url.port == 8443
    assert requests[0].headers["host"] == f"{HOST}:8443"
    assert requests[0].extensions["sni_hostname"] == HOST
    assert result.url == url
    assert result.body.source == url
    execution_id = (await events_of("execution.started"))[0].payload["execution_id"]
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert str(run.id) == execution_id
    assert run.status == RunStatus.SUCCEEDED
    assert run.finished_at is not None
    assert run.resource_usage["status_code"] == 200


async def test_explicit_localhost_allowlist_works(
    registry, approve, policy_factory, database, recorder
) -> None:
    permissions = PermissionService(
        database.sessions, recorder, policy_factory(allowed_hosts=["localhost"])
    )
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses("127.0.0.1", port),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="local")),
    )
    operation, grant = await _http_operation(
        registry, approve, permissions, url="http://localhost:11434/api/tags"
    )
    assert (await adapter.execute(operation, grant)).body.content == "local"


async def test_wildcard_does_not_allow_private_dns(
    registry, approve, policy_factory, database, recorder
) -> None:
    permissions = PermissionService(
        database.sessions, recorder, policy_factory(allowed_hosts=["*.example.com"])
    )
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses("127.0.0.1", port),
        transport=httpx.MockTransport(lambda request: pytest.fail("must not send")),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    with pytest.raises(OperationBlocked):
        await adapter.execute(operation, grant)


@pytest.mark.parametrize("answer", [[], _addresses("not-an-ip", 443)])
async def test_empty_or_invalid_dns_answers_blocked(
    answer, registry, approve, permissions, database, recorder
) -> None:
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: answer,
        transport=httpx.MockTransport(lambda request: pytest.fail("must not send")),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    with pytest.raises(OperationBlocked):
        await adapter.execute(operation, grant)


@pytest.mark.parametrize("status", [200, 500])
async def test_http_run_tracks_response_status(
    status, registry, approve, permissions, database, recorder
) -> None:
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    await adapter.execute(operation, grant)
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert run.status == (RunStatus.SUCCEEDED if status == 200 else RunStatus.FAILED)


async def test_http_cancel_is_durably_recorded(
    registry, approve, permissions, database, recorder
) -> None:
    sending = asyncio.Event()

    async def handler(request):
        sending.set()
        await asyncio.sleep(60)
        return httpx.Response(200)

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
        transport=httpx.MockTransport(handler),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    running = asyncio.create_task(adapter.execute(operation, grant))
    await asyncio.wait_for(sending.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert run.status == RunStatus.CANCELLED


@pytest.mark.parametrize(
    "restriction",
    [
        "unknown",
        "no network",
        "no_network=true",
        "read_only=false",
        "max_runtime_seconds=0",
        "max_runtime_seconds=-1",
        "max_runtime_seconds=nan",
        "max_runtime_seconds=inf",
        "max_runtime_seconds=oops",
        "max_output_bytes=0",
        "max_output_bytes=1.5",
        "allowed_hosts=",
        "allowed_hosts=*.example.com",
        "allowed_hosts=https://example.com",
    ],
)
def test_unknown_or_malformed_restrictions_rejected(restriction) -> None:
    with pytest.raises(InvalidSecurityDecision):
        parse_restrictions([restriction])


@pytest.mark.parametrize("restrictions", ["no_network", [True], ["no_network", "no_network"]])
def test_restriction_shape_and_duplicates_rejected(restrictions) -> None:
    with pytest.raises(InvalidSecurityDecision):
        parse_restrictions(restrictions)


async def test_invalid_stored_restriction_fails_closed(
    permissions, registry, database, clock
) -> None:
    operation = registry.build_operation(
        AgentId.FORGE, "run_process", {"argv": [sys.executable, "-V"]}
    )
    async with database.sessions() as session, session.begin():
        await SecurityDecisionRepository.stage(
            session,
            decision="approved",
            operation=operation.describe(),
            scope={"decided_by": "WARDEN", "restrictions": ["unknown"]},
            evidence=[],
            rationale="simulate a malformed historical row",
            input_hash=operation.input_hash,
            expires_at=clock.now + timedelta(minutes=5),
            created_at=clock.now,
        )
    with pytest.raises(OperationBlocked, match="unknown"):
        await permissions.authorize(operation)


async def test_runtime_and_output_limits_propagated_and_enforced(
    registry, approve, permissions, database, recorder
) -> None:
    adapter = SafeSubprocess(permissions, database.sessions, recorder)
    args = {
        "argv": [
            sys.executable,
            "-c",
            (
                "import sys, time; print('x'*100, flush=True); "
                "print('y'*100, file=sys.stderr, flush=True); time.sleep(60)"
            ),
        ]
    }
    operation = registry.build_operation(AgentId.FORGE, "run_process", args)
    await approve(operation, restrictions=["max_runtime_seconds=1", "max_output_bytes=16"])
    grant = await permissions.authorize(operation)
    assert grant.limits.max_runtime_seconds == 1
    result = await adapter.execute(operation, grant)
    assert result.timed_out
    assert result.duration_seconds < 3
    assert result.stdout_path.stat().st_size == 16
    assert result.stderr_path.stat().st_size == 16
    assert result.stdout_truncated and result.stderr_truncated


async def test_no_network_scrubs_all_proxy_variables(registry, approve, monkeypatch) -> None:
    for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.setenv(name, "not-a-real-proxy")
    args = {
        "argv": [
            sys.executable,
            "-c",
            "import os; print([name for name in os.environ if 'PROXY' in name.upper()])",
        ]
    }
    operation = registry.build_operation(AgentId.FORGE, "run_process", args)
    await approve(operation, restrictions=["no_network"])
    result = await registry.invoke(AgentId.FORGE, "run_process", args)
    assert result.succeeded
    assert result.stdout.content.strip() == "[]"


async def test_no_network_refuses_http(registry, approve, http_requests) -> None:
    args = {"url": f"https://{HOST}/data"}
    operation = registry.build_operation(AgentId.SCOUT, "http_request", args)
    await approve(operation, restrictions=["no_network"])
    with pytest.raises(OperationBlocked, match="no_network"):
        await registry.invoke(AgentId.SCOUT, "http_request", args)
    assert http_requests == []


async def test_read_only_refuses_file_write_and_append(
    registry, approve, roots, permissions
) -> None:
    path = roots["workspace"] / "restricted.txt"
    path.write_text("original")
    for append in (False, True):
        args = {"path": "restricted.txt", "content": "changed", "append": append}
        operation = registry.build_operation(AgentId.FORGE, "write_file", args)
        await approve(operation, restrictions=["read_only"])
        grant = await permissions.authorize(operation)
        with pytest.raises(OperationBlocked, match="read_only"):
            await SafeFiles(permissions).execute(operation, grant)
        assert path.read_text() == "original"


async def test_output_cap_applies_to_file_reads(registry, approve, roots) -> None:
    (roots["workspace"] / "input.txt").write_text("abcdefgh")
    args = {"path": "input.txt"}
    await approve(
        registry.build_operation(AgentId.LEDGER, "read_file", args),
        restrictions=["read_only", "max_output_bytes=3"],
    )
    result = await registry.invoke(AgentId.LEDGER, "read_file", args)
    assert result.content.content == "abc"
    assert result.truncated


async def test_output_cap_applies_to_http(registry, approve) -> None:
    args = {"url": f"https://{HOST}/big"}
    await approve(
        registry.build_operation(AgentId.SCOUT, "http_request", args),
        restrictions=["max_output_bytes=3"],
    )
    result = await registry.invoke(AgentId.SCOUT, "http_request", args)
    assert result.body.content == "xxx"
    assert result.truncated


async def test_http_runtime_limit_enforced(
    registry, approve, permissions, database, recorder
) -> None:
    async def handler(request):
        await asyncio.sleep(60)
        return httpx.Response(200)

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
        transport=httpx.MockTransport(handler),
    )
    operation = registry.build_operation(
        AgentId.SCOUT, "http_request", {"url": f"https://{HOST}/data"}
    )
    await approve(operation, restrictions=["max_runtime_seconds=0.1"])
    grant = await permissions.authorize(operation)
    started = time.monotonic()
    with pytest.raises(ExecutionFailed, match="TimeoutError"):
        await adapter.execute(operation, grant)
    assert time.monotonic() - started < 2
    async with database.sessions() as session:
        run = (await session.scalars(select(ExecutionRunRecord))).one()
    assert run.status == RunStatus.FAILED


async def test_allowed_hosts_restriction_cannot_expand_policy(
    registry, approve, permissions
) -> None:
    operation = registry.build_operation(
        AgentId.SCOUT, "http_request", {"url": f"https://{HOST}/data"}
    )
    await approve(operation, restrictions=[f"allowed_hosts={HOST},evil.example.net"])
    with pytest.raises(OperationBlocked, match="subset"):
        await permissions.authorize(operation)


async def test_allowed_hosts_enforced_for_initial_and_redirect_hosts(
    registry, approve, database, recorder, policy_factory, clock
) -> None:
    other = "other.example.com"
    permissions = PermissionService(
        database.sessions, recorder, policy_factory(allowed_hosts=[HOST, other])
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": f"https://{other}/private"})

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
        transport=httpx.MockTransport(handler),
    )
    operation = registry.build_operation(
        AgentId.SCOUT, "http_request", {"url": f"https://{HOST}/data"}
    )
    await approve(operation, restrictions=[f"allowed_hosts={other}"])
    grant = await permissions.authorize(operation)
    with pytest.raises(OperationBlocked, match="allowed_hosts"):
        await adapter.execute(operation, grant)
    assert requests == []
    clock.advance(timedelta(seconds=1))
    await approve(operation, restrictions=[f"allowed_hosts={HOST}"])
    grant = await permissions.authorize(operation)
    with pytest.raises(OperationBlocked, match="allowed_hosts"):
        await adapter.execute(operation, grant)
    assert len(requests) == 1


async def test_subprocess_allowed_hosts_fails_closed(registry, approve) -> None:
    args = {"argv": [sys.executable, "-V"]}
    operation = registry.build_operation(AgentId.FORGE, "run_process", args)
    await approve(operation, restrictions=[f"allowed_hosts={HOST}"])
    with pytest.raises(OperationBlocked, match="cannot be enforced"):
        await registry.invoke(AgentId.FORGE, "run_process", args)


async def test_blocked_events_include_input_hash_and_old_payload_still_valid(
    registry, events_of
) -> None:
    args = {"argv": [sys.executable, "-V"]}
    operation = registry.build_operation(AgentId.MUSE, "run_process", args)
    with pytest.raises(OperationBlocked):
        await registry.invoke(AgentId.MUSE, "run_process", args)
    assert (await events_of("security.operation_blocked"))[0].payload["input_hash"] == (
        operation.input_hash
    )
    legacy = build_event(
        "security.operation_blocked",
        source="test",
        actor="WARDEN",
        aggregate_type="operation",
        aggregate_id=operation.aggregate_id,
        payload={"operation": "old", "reason": "legacy"},
    )
    assert legacy.payload.input_hash is None


async def test_real_transport_connects_to_pin_and_reverifies_sni_per_host(
    registry, approve, database, recorder, policy_factory, monkeypatch
) -> None:
    other = "other.example.com"
    permissions = PermissionService(
        database.sessions, recorder, policy_factory(allowed_hosts=[HOST, other])
    )
    connects, tls_names, writes = [], [], []
    responses = iter(
        [
            (
                b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: https://"
                + other.encode()
                + b"/final\r\n\r\n"
            ),
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        ]
    )

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self, response):
            self.response = response

        async def read(self, max_bytes, timeout=None):
            data, self.response = self.response, b""
            return data

        async def write(self, buffer, timeout=None):
            writes.append(buffer)

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.check_hostname
            tls_names.append(server_hostname)
            return self

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, **kwargs):
            connects.append((host, port))
            return Stream(next(responses))

    real_init = httpcore.AsyncConnectionPool.__init__

    def init(pool, *args, **kwargs):
        kwargs["network_backend"] = Backend()
        real_init(pool, *args, **kwargs)

    monkeypatch.setattr(httpcore.AsyncConnectionPool, "__init__", init)
    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
    )
    operation, grant = await _http_operation(registry, approve, permissions)
    result = await adapter.execute(operation, grant)
    assert result.body.content == "ok"
    assert connects == [(PUBLIC_IP, 443), (PUBLIC_IP, 443)]
    assert tls_names == [HOST, other]
    wire = b"".join(writes).lower()
    assert b"host: api.example.com" in wire
    assert b"host: other.example.com" in wire


async def test_subprocess_spawn_failure_closes_containment(
    registry, approve, monkeypatch, database
) -> None:
    terminated = []
    terminate = ProcessContainment.terminate

    async def fail(*args, **kwargs):
        raise OSError("simulated startup failure")

    def record_terminate(self, pid):
        terminated.append(pid)
        terminate(self, pid)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    monkeypatch.setattr(ProcessContainment, "terminate", record_terminate)
    args = {"argv": [sys.executable, "-V"]}
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    with pytest.raises(ExecutionFailed):
        await registry.invoke(AgentId.FORGE, "run_process", args)
    assert terminated == [None]
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionRunRecord))).one().status == RunStatus.FAILED


async def test_redirect_origin_change_drops_sensitive_headers(
    registry, approve, permissions, database, recorder
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": f"http://{HOST}/final"})
        return httpx.Response(200)

    adapter = SafeHttp(
        permissions,
        database.sessions,
        recorder,
        resolver=lambda host, port: _addresses(PUBLIC_IP, port),
        transport=httpx.MockTransport(handler),
    )
    operation, grant = await _http_operation(
        registry,
        approve,
        permissions,
        headers={"Authorization": "test-placeholder", "Cookie": "test-placeholder"},
    )
    await adapter.execute(operation, grant)
    assert "authorization" in requests[0].headers
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers


async def test_containment_failure_invokes_psutil_cleanup_and_records_failure(
    registry, approve, monkeypatch, database, roots
) -> None:
    from aether.tools.adapters import subprocess as adapter_module

    # Use a known direct child; no real process should be orphaned by the simulated OS error.
    calls = []
    fallback = adapter_module.kill_process_tree
    terminate = ProcessContainment.terminate

    def fail_once(self, pid):
        if pid is not None and not calls:
            calls.append(pid)
            raise OSError("simulated OS cleanup failure")
        terminate(self, pid)

    def record_fallback(pid):
        calls.append(("fallback", pid))
        return fallback(pid)

    monkeypatch.setattr(ProcessContainment, "terminate", fail_once)
    monkeypatch.setattr(adapter_module, "kill_process_tree", record_fallback)
    args = {
        "argv": [sys.executable, "-c", TREE + "\ntime.sleep(60)"],
        "timeout_seconds": 1,
    }
    await approve(registry.build_operation(AgentId.FORGE, "run_process", args))
    with pytest.raises(ExecutionFailed):
        await registry.invoke(AgentId.FORGE, "run_process", args)
    assert ("fallback", calls[0]) in calls
    await _assert_tree_gone(roots)
    async with database.sessions() as session:
        assert (await session.scalars(select(ExecutionRunRecord))).one().status == RunStatus.FAILED
