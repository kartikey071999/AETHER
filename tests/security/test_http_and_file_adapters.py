import pytest

from aether.communication.messages import AgentId
from aether.security import BlockReason, OperationBlocked
from aether.tools import ToolInputError

URL = "https://api.example.com/data"


async def _approved_http(registry, approve, args: dict):
    operation = registry.build_operation(AgentId.SCOUT, "http_request", args)
    await approve(operation)
    return await registry.invoke(AgentId.SCOUT, "http_request", args)


async def test_http_to_allowlisted_host_returns_untrusted_body(
    registry, approve, http_requests, events_of
) -> None:
    result = await _approved_http(registry, approve, {"url": URL})
    assert result.status_code == 200
    assert '"ok":true' in result.body.content.replace(" ", "")
    assert result.body.source == URL
    assert len(http_requests) == 1
    assert len(await events_of("execution.started")) == 1
    finished = await events_of("execution.finished")
    assert finished[0].payload["success"] is True


async def test_http_to_disallowed_host_is_blocked_without_a_request(
    registry, approve, http_requests
) -> None:
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.SCOUT, "http_request", {"url": "https://evil.example.net/"})
    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    assert http_requests == []


async def test_redirect_to_disallowed_host_is_refused(
    registry, approve, http_requests, events_of
) -> None:
    url = "https://api.example.com/redirect-out"
    with pytest.raises(OperationBlocked) as raised:
        await _approved_http(
            registry, approve, {"url": url, "headers": {"Authorization": "Bearer fake"}}
        )
    assert "redirect refused" in raised.value.detail
    assert len(http_requests) == 1
    assert http_requests[0].url.host == "93.184.216.34"
    assert http_requests[0].url.path == "/redirect-out"
    assert http_requests[0].headers["host"] == "api.example.com"
    assert http_requests[0].extensions["sni_hostname"] == "api.example.com"
    finished = await events_of("execution.finished")
    assert finished[0].payload["success"] is False


async def test_same_host_redirect_is_followed(registry, approve) -> None:
    result = await _approved_http(registry, approve, {"url": "https://api.example.com/redirect-in"})
    assert result.redirects == ("https://api.example.com/data",)
    assert result.url == "https://api.example.com/data"


async def test_http_body_is_capped(registry, approve, policy) -> None:
    result = await _approved_http(registry, approve, {"url": "https://api.example.com/big"})
    assert result.truncated
    assert len(result.body.content) == policy.max_response_bytes


async def test_http_requires_absolute_http_url(registry) -> None:
    for url in ("file:///etc/passwd", "/relative", "ftp://api.example.com/x"):
        with pytest.raises(ToolInputError):
            registry.build_operation(AgentId.SCOUT, "http_request", {"url": url})


async def test_agent_without_network_permission_is_blocked(registry, approve) -> None:
    args = {"url": URL}
    operation = registry.build_operation(AgentId.MUSE, "http_request", args)
    await approve(operation)
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.MUSE, "http_request", args)
    assert raised.value.reason is BlockReason.AGENT_NOT_PERMITTED


async def test_file_write_and_read_inside_workspace(registry, roots) -> None:
    written = await registry.invoke(
        AgentId.FORGE, "write_file", {"path": "out/notes.txt", "content": "hello"}
    )
    assert written.path == (roots["workspace"] / "out" / "notes.txt").resolve()
    read = await registry.invoke(AgentId.LEDGER, "read_file", {"path": "out/notes.txt"})
    assert read.content.content == "hello"
    assert read.content.source.startswith("file:")


@pytest.mark.parametrize(
    "path", ["..\\..\\escape.txt", "../../escape.txt", "out/../../../escape.txt"]
)
async def test_file_path_traversal_is_blocked(registry, roots, path: str) -> None:
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.FORGE, "write_file", {"path": path, "content": "x"})
    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    assert not (roots["workspace"].parent.parent / "escape.txt").exists()


async def test_file_absolute_path_outside_roots_is_blocked(registry, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(OperationBlocked):
        await registry.invoke(AgentId.LEDGER, "read_file", {"path": str(outside)})


async def test_agents_without_file_write_permission_are_blocked(registry) -> None:
    for agent in (AgentId.MUSE, AgentId.ROGUE, AgentId.LEDGER, AgentId.WARDEN):
        with pytest.raises(OperationBlocked) as raised:
            await registry.invoke(agent, "write_file", {"path": "a.txt", "content": "x"})
        assert raised.value.reason is BlockReason.AGENT_NOT_PERMITTED
