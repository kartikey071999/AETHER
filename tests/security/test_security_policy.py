import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.security import (
    Operation,
    OperationKind,
    PolicyMutationError,
    SecurityPolicy,
    SecuritySettings,
    load_security_policy,
)


def _operation(**overrides) -> Operation:
    fields = {
        "kind": OperationKind.SUBPROCESS,
        "target": "python",
        "arguments": {"argv": ["python", "-V"], "options": {"a": 1, "b": 2}},
        "cwd": "workspace",
        "requested_by": AgentId.FORGE,
    }
    fields.update(overrides)
    return Operation(**fields)


def test_input_hash_is_stable_across_dict_ordering_and_host_order() -> None:
    first = _operation(arguments={"x": 1, "y": {"b": 2, "a": 1}}, hosts=("B.example", "a.example"))
    second = _operation(
        arguments={"y": {"a": 1, "b": 2}, "x": 1}, hosts=("a.example.", "b.example")
    )
    assert first.input_hash == second.input_hash
    assert len(first.input_hash) == 64
    assert first.aggregate_id == second.aggregate_id


def test_input_hash_changes_with_any_security_relevant_field() -> None:
    base = _operation()
    variants = [
        _operation(target="python3"),
        _operation(arguments={"argv": ["python", "-c", "print(1)"]}),
        _operation(cwd="elsewhere"),
        _operation(hosts=("api.example.com",)),
        _operation(requested_by=AgentId.LEDGER),
        _operation(kind=OperationKind.PACKAGE_INSTALL),
    ]
    assert len({base.input_hash, *(variant.input_hash for variant in variants)}) == 7


def test_in_place_mutation_changes_the_hash() -> None:
    operation = _operation()
    original = operation.input_hash
    operation.arguments["argv"].append("--evil")
    assert operation.input_hash != original


def test_operation_rejects_non_json_arguments_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _operation(arguments={"value": object()})
    with pytest.raises(ValidationError):
        _operation(arguments={"value": float("nan")})
    operation = _operation()
    with pytest.raises(ValidationError):
        operation.target = "other"


def test_policy_cannot_be_mutated(policy: SecurityPolicy) -> None:
    with pytest.raises(PolicyMutationError):
        policy.allowed_hosts = frozenset({"evil.example"})
    with pytest.raises(PolicyMutationError):
        policy.approval_required_kinds = frozenset()
    with pytest.raises(PolicyMutationError):
        del policy.filesystem_roots
    with pytest.raises(PolicyMutationError):
        policy.brand_new_attribute = True
    with pytest.raises(TypeError):
        policy.agent_permissions[AgentId.MUSE] = frozenset(OperationKind)  # type: ignore[index]
    with pytest.raises(AttributeError):
        policy.allowed_hosts.add("evil.example")  # type: ignore[attr-defined]
    assert not policy.is_host_allowed("evil.example")


def test_default_policy_from_settings_is_default_deny(tmp_path: Path) -> None:
    settings = AetherSettings(
        _env_file=None,
        workspace_dir=tmp_path / "w",
        artifact_dir=tmp_path / "a",
        data_dir=tmp_path / "d",
    )
    policy = load_security_policy(settings, SecuritySettings(_env_file=None))
    assert policy.allowed_executables == frozenset()
    assert policy.allowed_hosts == frozenset()
    assert policy.resolve_executable(sys.executable) is None
    for kind in (OperationKind.SUBPROCESS, OperationKind.HTTP_REQUEST, OperationKind.REPO_CLONE):
        assert policy.requires_approval(kind)
    assert policy.max_approval_ttl == timedelta(hours=1)
    assert policy.confine_path(tmp_path / "w" / "file.txt") is not None
    assert policy.confine_path(tmp_path / "elsewhere.txt") is None


def test_security_settings_load_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_SECURITY_ALLOWED_HOSTS", '["PyPI.org", "*.example.com"]')
    monkeypatch.setenv(
        "AETHER_SECURITY_ALLOWED_EXECUTABLES", f'["{Path(sys.executable).as_posix()}"]'
    )
    monkeypatch.setenv("AETHER_SECURITY_MAX_APPROVAL_TTL_SECONDS", "120")
    settings = AetherSettings(
        _env_file=None,
        workspace_dir=tmp_path / "w",
        artifact_dir=tmp_path / "a",
        data_dir=tmp_path / "d",
    )
    policy = load_security_policy(settings, SecuritySettings(_env_file=None))
    assert policy.allowed_hosts == frozenset({"pypi.org", "*.example.com"})
    assert policy.resolve_executable(sys.executable) is not None
    assert policy.max_approval_ttl == timedelta(seconds=120)


def test_credential_and_config_kinds_always_need_warden_and_user(roots) -> None:
    policy = SecurityPolicy(
        workspace_dir=roots["workspace"],
        artifact_dir=roots["artifacts"],
        data_dir=roots["data"],
        approval_required_kinds=(),
        user_authorization_required_kinds=(),
    )
    for kind in (OperationKind.CONFIG_CHANGE, OperationKind.CREDENTIAL_ACCESS):
        assert policy.requires_approval(kind)
        assert policy.requires_user_authorization(kind)
    assert not policy.requires_approval(OperationKind.SUBPROCESS)


def test_agent_actions_map_to_least_privilege_kinds(policy: SecurityPolicy) -> None:
    assert policy.permits(AgentId.FORGE, OperationKind.SUBPROCESS)
    assert policy.permits(AgentId.LEDGER, OperationKind.SUBPROCESS)
    assert policy.permits(AgentId.SCOUT, OperationKind.HTTP_REQUEST)
    assert not policy.permits(AgentId.MUSE, OperationKind.SUBPROCESS)
    assert not policy.permits(AgentId.ROGUE, OperationKind.FILE_WRITE)
    assert not policy.permits(AgentId.WARDEN, OperationKind.SUBPROCESS)
    for agent in AgentId:
        assert not policy.permits(agent, OperationKind.CREDENTIAL_ACCESS)
        assert not policy.permits(agent, OperationKind.CONFIG_CHANGE)


def test_path_confinement_blocks_traversal(policy: SecurityPolicy, roots) -> None:
    workspace = roots["workspace"]
    assert policy.confine_path("notes/a.txt") == (workspace / "notes" / "a.txt").resolve()
    assert policy.confine_path("../outside.txt") is None
    assert policy.confine_path(workspace / ".." / ".." / "outside.txt") is None
    assert policy.confine_path(str(roots["data"] / "x.json")) is not None
    assert policy.confine_path("bad\x00name") is None


def test_symlink_escape_is_blocked(policy: SecurityPolicy, roots, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = roots["workspace"] / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not permitted on this host")
    assert policy.confine_path(link / "secret.txt") is None


def test_host_allowlist_supports_exact_and_wildcard(policy_factory) -> None:
    policy = policy_factory(allowed_hosts=["api.example.com", "*.pkg.example"])
    assert policy.is_host_allowed("API.example.com.")
    assert policy.is_host_allowed("files.pkg.example")
    assert not policy.is_host_allowed("pkg.example")
    assert not policy.is_host_allowed("api.example.com.evil.net")


def test_static_policy_checks(policy: SecurityPolicy) -> None:
    ok = Operation(
        kind=OperationKind.HTTP_REQUEST,
        target="https://api.example.com/v1",
        hosts=("api.example.com",),
        requested_by=AgentId.SCOUT,
    )
    assert policy.static_violation(ok) is None
    smuggled = ok.model_copy(update={"target": "https://evil.example.net/"})
    assert "not allowlisted" in (policy.static_violation(smuggled) or "")
    no_host = Operation(
        kind=OperationKind.NETWORK_SCAN, target="local network", requested_by=AgentId.SCOUT
    )
    assert "declare" in (policy.static_violation(no_host) or "")
    clone = Operation(
        kind=OperationKind.REPO_CLONE,
        target="git@github.com:org/repo.git",
        requested_by=AgentId.FORGE,
    )
    assert "github.com" in (policy.static_violation(clone) or "")
