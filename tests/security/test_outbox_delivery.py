from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from aether.communication.messages import AgentId
from aether.security import Operation, OperationBlocked, OperationKind, SecurityPolicy
from aether.security.errors import BlockReason
from aether.security.policy import OUTBOX_ARGUMENT, OUTBOX_MAX_BYTES
from aether.tools import ToolRegistry

TOOL = "deliver_outbox_message"


def outbox_name(stamp: str = "20261031T000000Z") -> str:
    return f"{stamp}-{uuid4()}.md"


@pytest.fixture
def outbox(roots: dict[str, Path]) -> Path:
    return roots["data"] / "outbox"


async def deliver(registry: ToolRegistry, path: Path | str, content: str = "hello", **extra):
    return await registry.invoke(
        AgentId.MUSE, TOOL, {"path": str(path), "content": content, **extra}
    )


async def test_muse_default_policy_delivers_one_new_file(registry, outbox):
    path = outbox / outbox_name()

    receipt = await deliver(registry, path, "# Update\nAll good.")

    assert receipt.path == path.resolve()
    assert path.read_text(encoding="utf-8") == "# Update\nAll good."
    assert receipt.bytes_written == len(b"# Update\nAll good.")
    assert [p.name for p in outbox.iterdir()] == [path.name]


async def test_muse_policy_grants_only_the_narrow_write(policy: SecurityPolicy):
    assert policy.permits(AgentId.MUSE, OperationKind.FILE_WRITE)
    assert not policy.permits(AgentId.MUSE, OperationKind.FILE_READ)
    assert not policy.permits(AgentId.MUSE, OperationKind.SUBPROCESS)
    assert not policy.permits(AgentId.MUSE, OperationKind.HTTP_REQUEST)


@pytest.mark.parametrize("agent", [AgentId.FORGE, AgentId.ROGUE, AgentId.WARDEN, AgentId.SCOUT])
async def test_other_agents_cannot_use_outbox_delivery(registry, outbox, agent):
    path = outbox / outbox_name()
    with pytest.raises(OperationBlocked):
        await registry.invoke(agent, TOOL, {"path": str(path), "content": "x"})
    assert not path.exists()


async def test_existing_file_is_never_overwritten(registry, outbox):
    path = outbox / outbox_name()
    await deliver(registry, path, "first")

    with pytest.raises(OperationBlocked) as raised:
        await deliver(registry, path, "second")

    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    assert path.read_text(encoding="utf-8") == "first"
    assert [p.name for p in outbox.iterdir()] == [path.name]


async def test_identical_retry_is_idempotent(registry, outbox):
    path = outbox / outbox_name()
    first = await deliver(registry, path, "same")
    second = await deliver(registry, path, "same")

    assert first == second
    assert path.read_text(encoding="utf-8") == "same"


def _cases(roots: dict[str, Path]) -> list[Callable[[Path], str]]:
    return [
        lambda outbox: str(outbox / "sub" / outbox_name()),
        lambda outbox: str(outbox / ".." / outbox_name()),
        lambda outbox: str(outbox / ".." / "outbox" / ".." / "elsewhere" / outbox_name()),
        lambda outbox: str(roots["workspace"] / outbox_name()),
        lambda outbox: str(roots["artifacts"] / outbox_name()),
        lambda outbox: str(outbox.parent / outbox_name()),
        lambda outbox: str(outbox / outbox_name().replace(".md", ".txt")),
        lambda outbox: str(outbox / outbox_name().replace(".md", ".md.md")),
        lambda outbox: str(outbox / "message.md"),
        lambda outbox: str(outbox / f"{uuid4()}.md"),
        lambda outbox: str(outbox / ("../" + outbox_name())),
        lambda outbox: f"outbox/{outbox_name()}",
        lambda outbox: outbox_name(),
    ]


@pytest.mark.parametrize("index", range(13))
async def test_paths_outside_the_single_file_contract_are_blocked(registry, roots, outbox, index):
    target = _cases(roots)[index](outbox)

    with pytest.raises(OperationBlocked) as raised:
        await deliver(registry, target)

    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    assert not roots["workspace"].joinpath(Path(target).name).exists()
    assert not outbox.exists() or not list(outbox.iterdir())


async def test_oversized_and_empty_messages_are_rejected(registry, outbox):
    from aether.tools import ToolInputError

    with pytest.raises((OperationBlocked, ToolInputError)):
        await deliver(registry, outbox / outbox_name(), "x" * (OUTBOX_MAX_BYTES + 1))
    with pytest.raises((OperationBlocked, ToolInputError)):
        await deliver(registry, outbox / outbox_name(), "")
    # Multi-byte text counts in bytes, not characters.
    with pytest.raises(OperationBlocked):
        await deliver(registry, outbox / outbox_name(), "é" * (OUTBOX_MAX_BYTES // 2 + 1))


async def test_tool_rejects_append_and_extra_arguments(registry, outbox):
    from aether.tools import ToolInputError

    with pytest.raises(ToolInputError):
        await deliver(registry, outbox / outbox_name(), append=True)


async def test_generic_write_file_is_still_denied_for_muse(registry, roots, outbox):
    targets = [
        outbox / outbox_name(),
        roots["workspace"] / "notes.md",
        roots["data"] / "other.md",
        roots["artifacts"] / "a.txt",
    ]
    for target in targets:
        with pytest.raises(OperationBlocked) as raised:
            await registry.invoke(AgentId.MUSE, "write_file", {"path": str(target), "content": "x"})
        assert raised.value.reason is BlockReason.AGENT_NOT_PERMITTED
        assert not target.exists()


async def test_muse_cannot_forge_the_outbox_marker_elsewhere(policy: SecurityPolicy, roots):
    forged = Operation(
        kind=OperationKind.FILE_WRITE,
        target=str(roots["workspace"] / outbox_name()),
        arguments={"content": "x", OUTBOX_ARGUMENT: True},
        requested_by=AgentId.MUSE,
    )
    assert policy.static_violation(forged) is not None
    assert not policy.permits_operation(forged.model_copy(update={"arguments": {"content": "x"}}))


async def test_other_agents_keep_their_generic_write_permission(registry, roots):
    receipt = await registry.invoke(
        AgentId.FORGE, "write_file", {"path": "out/notes.txt", "content": "hello"}
    )
    assert receipt.path.read_text(encoding="utf-8") == "hello"
