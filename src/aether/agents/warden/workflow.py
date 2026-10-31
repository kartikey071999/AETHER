"""WARDEN's deterministic security rules.

The outcome of a review is decided here, in code, from the operation, the immutable policy and
the requirements ledger. Evidence supplied with a request is untrusted data: it can satisfy a
provenance check (a source, an integrity hash, a version pin) or add risk flags, but nothing
written inside it can turn a rejection into an approval or relax a restriction.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aether.communication.messages import AgentId
from aether.security import DecisionOutcome
from aether.security.operations import Operation, OperationKind
from aether.security.policy import NETWORK_KINDS, SecurityPolicy, url_host
from aether.security.untrusted import UntrustedContent
from aether.storage.models import RequirementStatus

__all__ = [
    "ACTIONABLE_REQUIREMENT_STATES",
    "Assessment",
    "Check",
    "InstallSpec",
    "Provenance",
    "RequirementFacts",
    "evaluate",
    "evidence_flags",
    "install_spec",
    "provenance",
    "restrictions_for",
]

_K = OperationKind

# A declared requirement in one of these states justifies installing or changing things.
ACTIONABLE_REQUIREMENT_STATES = frozenset(
    {RequirementStatus.APPROVED, RequirementStatus.IN_PROGRESS, RequirementStatus.SATISFIED}
)
_DEAD_REQUIREMENT_STATES = frozenset({RequirementStatus.REJECTED, RequirementStatus.SUPERSEDED})
# Kinds that must be justified by an actionable requirement in the ledger.
_REQUIREMENT_GATED_KINDS = frozenset(
    {_K.PACKAGE_INSTALL, _K.REPO_CLONE, _K.CONFIG_CHANGE, _K.CREDENTIAL_ACCESS, _K.NETWORK_SCAN}
)

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"})
_INLINE_SCRIPT_FLAGS = frozenset({"-c", "/c", "/k", "-command", "-encodedcommand", "-e", "-ec"})
_COMMAND_PATTERNS = frozenset({"pipe_to_shell", "destructive_command"})
_PACKAGE_MANAGERS: Mapping[str, frozenset[str]] = {
    "pip": frozenset({"install"}),
    "pip3": frozenset({"install"}),
    "uv": frozenset({"add", "install"}),
    "npm": frozenset({"install", "i", "add"}),
    "pnpm": frozenset({"install", "i", "add"}),
    "yarn": frozenset({"install", "add"}),
    "poetry": frozenset({"add", "install"}),
    "conda": frozenset({"install"}),
    "gem": frozenset({"install"}),
    "cargo": frozenset({"install", "add"}),
    "go": frozenset({"get", "install"}),
    "apt": frozenset({"install"}),
    "apt-get": frozenset({"install"}),
    "brew": frozenset({"install"}),
    "choco": frozenset({"install"}),
    "winget": frozenset({"install"}),
}
# Options whose value is the next argv token (so it is not mistaken for a package name).
_VALUE_OPTIONS = frozenset(
    {
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--registry",
        "-c",
        "--constraint",
        "-t",
        "--target",
        "--prefix",
        "--root",
        "--python",
        "-p",
        "-b",
        "--branch",
        "--depth",
        "--origin",
        "-o",
    }
)
_REQUIREMENTS_FILE_OPTIONS = frozenset({"-r", "--requirement", "-e", "--editable"})
_SOURCE_OPTIONS = frozenset({"-i", "--index-url", "--registry"})
_FLOATING_REFS = frozenset({"", "head", "main", "master", "trunk", "develop", "latest", "*"})
_EXACT_VERSION = re.compile(r"^v?[0-9][0-9A-Za-z.+!_-]*$")
_SHA256 = re.compile(r"^(?:sha256[:=-])?(?P<hex>[0-9a-fA-F]{64})$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_PIN_SPLIT = re.compile(r"(===|==|~=|>=|<=|!=|>|<|@)")
_SOURCE_KEYS = ("source", "publisher", "index_url", "registry", "repository", "url", "homepage")
_INTEGRITY_KEYS = ("sha256", "integrity", "hash", "digest", "checksum")
_COMMIT_KEYS = ("commit", "revision", "rev")
_REF_KEYS = ("tag", "version", "ref")
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".ssh",
        ".aws",
        ".azure",
        ".gnupg",
        ".docker",
        ".kube",
        ".netrc",
        ".pypirc",
        ".npmrc",
        ".git-credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
    }
)
_HARMLESS_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")
_EVIDENCE_TEXT_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class RequirementFacts:
    """What the ledger says about the requirement an operation declares."""

    requirement_id: UUID
    status: RequirementStatus
    text: str = ""

    def mentions(self, name: str) -> bool:
        wanted = _normalize_name(name)
        return bool(wanted) and wanted in _normalize_name(self.text)


@dataclass(frozen=True, slots=True)
class InstallSpec:
    """An operation that brings third-party code onto the machine."""

    kind: str  # "package" or "clone"
    packages: tuple[str, ...] = ()
    pins: tuple[str | None, ...] = ()
    unpinnable: bool = False
    source: str | None = None
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str | None
    integrity: str | None
    version: str | None

    def missing(self) -> tuple[str, ...]:
        names = (
            ("source/publisher", self.source),
            ("integrity hash", self.integrity),
            ("version pin", self.version),
        )
        return tuple(name for name, value in names if not value)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def as_evidence(self) -> dict[str, Any]:
        return {
            "type": "warden_check",
            "check": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Assessment:
    """WARDEN's code-made decision for one operation."""

    outcome: DecisionOutcome
    rationale: str
    checks: tuple[Check, ...]
    restrictions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    version_constraint: str | None = None
    needs_user_authorization: bool = False
    provenance: Provenance | None = None

    @property
    def approved(self) -> bool:
        return self.outcome is DecisionOutcome.APPROVED


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.\s]+", "-", value.lower())


def _basename(path: str) -> str:
    name = re.split(r"[\\/]", path.strip())[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _argv(operation: Operation) -> list[str]:
    argv = operation.arguments.get("argv")
    if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
        return list(argv)
    return [operation.target] if operation.kind is _K.SUBPROCESS else []


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _sensitive_component(value: str) -> str | None:
    for component in re.split(r"[\\/]", value):
        # Option values such as --file=.env still name the file.
        name = component.lower().rsplit("=", 1)[-1].strip("'\"")
        if name in _SENSITIVE_NAMES:
            return name
        if name.startswith(".env.") and not name.endswith(_HARMLESS_ENV_SUFFIXES):
            return name
    return None


def _sensitive_reference(operation: Operation) -> str | None:
    candidates = [operation.target, *(operation.cwd or "",), *_strings(operation.arguments)]
    for candidate in candidates:
        hit = _sensitive_component(candidate)
        if hit is not None:
            return hit
    return None


def _inline_shell(operation: Operation) -> bool:
    argv = _argv(operation)
    if not argv or (
        _basename(argv[0]) not in _SHELLS and _basename(operation.target) not in _SHELLS
    ):
        return False
    return any(token.lower() in _INLINE_SCRIPT_FLAGS for token in argv[1:])


def _command_patterns(operation: Operation) -> tuple[str, ...]:
    if operation.kind is not _K.SUBPROCESS:
        return ()
    text = " ".join(_argv(operation))
    flags = UntrustedContent(source="operation:argv", content=text).suspicious_patterns()
    return tuple(flag for flag in flags if flag in _COMMAND_PATTERNS)


def _split_pin(token: str) -> tuple[str, str | None]:
    """``name==1.2`` / ``name@1.2`` / ``@scope/name@1.2`` -> (name, exact version or None)."""
    scoped = token.startswith("@")
    body = token[1:] if scoped else token
    parts = _PIN_SPLIT.split(body, maxsplit=1)
    name = ("@" if scoped else "") + parts[0].split("[", 1)[0]
    if len(parts) < 3:
        return name, None
    operator, version = parts[1], parts[2].strip()
    if operator not in {"==", "===", "@"} or not _EXACT_VERSION.match(version):
        return name, None
    if "*" in version or "," in version:
        return name, None
    return name, version


def _package_tokens(arguments: Sequence[str]) -> tuple[list[str], bool, str | None]:
    """Package tokens after the install verb, whether a requirements file is used, and source."""
    packages: list[str] = []
    unpinnable = False
    source: str | None = None
    skip_next = False
    for index, token in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        lowered = token.lower()
        option, _, inline_value = lowered.partition("=")
        if option in _REQUIREMENTS_FILE_OPTIONS:
            unpinnable = True
            skip_next = not inline_value
            continue
        if option in _VALUE_OPTIONS:
            value = inline_value or (arguments[index + 1] if index + 1 < len(arguments) else "")
            if option in _SOURCE_OPTIONS and value:
                source = token.partition("=")[2] or value
            skip_next = not inline_value
            continue
        if token.startswith("-"):
            continue
        if ("/" in token and not token.startswith("@")) or "\\" in token or token == ".":
            # Local paths and URLs install unreviewable, unpinned content.
            unpinnable = True
            continue
        packages.append(token)
    return packages, unpinnable, source


def install_spec(operation: Operation) -> InstallSpec | None:
    """Describe what third-party code the operation installs or clones, if any."""
    arguments = operation.arguments
    if operation.kind is _K.REPO_CLONE:
        ref = next(
            (str(arguments[key]) for key in (*_COMMIT_KEYS, *_REF_KEYS) if arguments.get(key)),
            None,
        )
        return InstallSpec(kind="clone", source=operation.target, ref=ref)
    if operation.kind is _K.PACKAGE_INSTALL:
        name = str(arguments.get("package") or arguments.get("name") or operation.target)
        package, pin = _split_pin(name)
        version = arguments.get("version")
        if isinstance(version, str) and _EXACT_VERSION.match(version) and "*" not in version:
            pin = version
        source = next(
            (
                str(arguments[key])
                for key in ("index_url", "registry", "source")
                if arguments.get(key)
            ),
            None,
        )
        return InstallSpec(kind="package", packages=(package,), pins=(pin,), source=source)
    if operation.kind is not _K.SUBPROCESS:
        return None
    argv = _argv(operation)
    tokens = [_basename(token) if index == 0 else token for index, token in enumerate(argv)]
    lowered = [token.lower() for token in tokens]
    if "git" in lowered[:1] and "clone" in lowered:
        rest = argv[lowered.index("clone") + 1 :]
        url = next((item for item in rest if not item.startswith("-")), None)
        ref = None
        for flag in ("-b", "--branch", "--revision"):
            if flag in rest and rest.index(flag) + 1 < len(rest):
                ref = rest[rest.index(flag) + 1]
        return InstallSpec(kind="clone", source=url, ref=ref)
    for index, token in enumerate(lowered):
        verbs = _PACKAGE_MANAGERS.get(token)
        if verbs is None:
            continue
        rest_lowered = lowered[index + 1 :]
        verb_index = next((i for i, item in enumerate(rest_lowered) if item in verbs), None)
        if verb_index is None:
            # ``uv pip install`` and ``python -m pip install`` nest the real verb.
            continue
        rest = argv[index + 1 + verb_index + 1 :]
        names, unpinnable, source = _package_tokens(rest)
        split = [_split_pin(name) for name in names]
        return InstallSpec(
            kind="package",
            packages=tuple(name for name, _ in split),
            pins=tuple(pin for _, pin in split),
            unpinnable=unpinnable or not names,
            source=source,
        )
    return None


def _evidence_value(evidence: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> str | None:
    for item in evidence:
        if not isinstance(item, Mapping) or item.get("type") == "untrusted_content":
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _integrity(value: str | None) -> str | None:
    if value is None:
        return None
    match = _SHA256.match(value.strip())
    return f"sha256:{match['hex'].lower()}" if match else None


def provenance(
    operation: Operation, spec: InstallSpec, evidence: Sequence[Mapping[str, Any]]
) -> Provenance:
    """Source, integrity and version pin. The pin must be in the operation itself.

    Evidence may name the source and integrity hash, but a version that only appears in
    evidence does not bind what runs, so it does not count as a pin.
    """
    source = spec.source or _evidence_value(evidence, _SOURCE_KEYS)
    integrity = _integrity(_evidence_value(evidence, _INTEGRITY_KEYS))
    if integrity is None:
        integrity = _integrity(
            next(
                (
                    str(operation.arguments[key])
                    for key in _INTEGRITY_KEYS
                    if isinstance(operation.arguments.get(key), str)
                ),
                None,
            )
        )
    version: str | None
    if spec.kind == "clone":
        ref = (spec.ref or "").strip()
        commit = ref if _COMMIT.match(ref) else _evidence_value(evidence, _COMMIT_KEYS)
        if integrity is None and commit is not None and _COMMIT.match(commit):
            integrity = f"git:{commit.lower()}"
        version = ref if ref.lower() not in _FLOATING_REFS else None
    elif spec.unpinnable or not spec.pins or any(pin is None for pin in spec.pins):
        version = None
    else:
        version = ",".join(str(pin) for pin in spec.pins)
    return Provenance(source=source, integrity=integrity, version=version)


def _evidence_text(evidence: Sequence[Mapping[str, Any]]) -> str:
    text = "\n".join(_strings(list(evidence)))
    return text[:_EVIDENCE_TEXT_LIMIT]


def evidence_flags(evidence: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Instruction-like patterns found in evidence. They are reported, never obeyed."""
    text = _evidence_text(evidence)
    if not text:
        return ()
    return UntrustedContent(source="security.evaluate:evidence", content=text).suspicious_patterns()


def _hosts(operation: Operation) -> tuple[str, ...]:
    hosts = set(operation.hosts)
    if operation.kind in NETWORK_KINDS and (host := url_host(operation.target)) is not None:
        hosts.add(host)
    return tuple(sorted(hosts))


def _seconds(value: float) -> str:
    return f"{value:g}"


def restrictions_for(
    operation: Operation, policy: SecurityPolicy, spec: InstallSpec | None
) -> tuple[str, ...]:
    """Least-privilege execution limits from the valid restriction vocabulary."""
    kind = operation.kind
    restrictions: list[str] = []
    if kind is _K.SUBPROCESS:
        if spec is None:
            restrictions.append("no_network")
        timeout = policy.subprocess_timeout_seconds
        requested = operation.arguments.get("timeout_seconds")
        if isinstance(requested, int | float) and 0 < requested < timeout:
            timeout = float(requested)
        restrictions.append(f"max_runtime_seconds={_seconds(timeout)}")
        restrictions.append(f"max_output_bytes={policy.max_output_bytes}")
    elif kind in (_K.PACKAGE_INSTALL, _K.REPO_CLONE):
        restrictions.append(f"max_runtime_seconds={_seconds(policy.subprocess_timeout_seconds)}")
    elif kind is _K.HTTP_REQUEST:
        restrictions.append(f"max_runtime_seconds={_seconds(policy.http_timeout_seconds)}")
        restrictions.append(f"max_output_bytes={policy.max_response_bytes}")
    elif kind is _K.FILE_READ:
        restrictions.append("read_only")
        restrictions.append(f"max_output_bytes={policy.max_output_bytes}")
    hosts = _hosts(operation)
    if kind in NETWORK_KINDS and hosts and all(policy.is_host_allowed(host) for host in hosts):
        restrictions.append(f"allowed_hosts={','.join(hosts)}")
    return tuple(restrictions)


def _within(path: str, roots: Sequence[Any], policy: SecurityPolicy) -> bool:
    resolved = policy.confine_path(path)
    return resolved is not None and any(
        resolved == root or resolved.is_relative_to(root) for root in roots
    )


def _risks(
    operation: Operation,
    policy: SecurityPolicy,
    spec: InstallSpec | None,
    flags: Sequence[str],
) -> list[str]:
    risks: list[str] = []
    hosts = _hosts(operation)
    if hosts:
        risks.append(f"contacts remote hosts: {', '.join(hosts)}")
    if spec is not None and spec.kind == "package":
        risks.append(f"installs third-party packages: {', '.join(spec.packages) or 'unknown'}")
    elif spec is not None:
        risks.append(f"clones third-party source: {spec.source or 'unknown'}")
    if operation.kind is _K.SUBPROCESS:
        risks.append("runs a local process; the decision covers only this exact argv and cwd")
    if operation.kind is _K.FILE_WRITE:
        risks.append(f"writes {operation.target}")
    if policy.requires_user_authorization(operation.kind):
        risks.append("high-impact operation: explicit user authorization is required")
    if flags:
        risks.append(
            "evidence contains instruction-like text treated as data: " + ", ".join(sorted(flags))
        )
    return risks


def _decide(
    operation: Operation,
    policy: SecurityPolicy,
    requirement: RequirementFacts | None,
    spec: InstallSpec | None,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[DecisionOutcome, list[Check], Provenance | None]:
    checks: list[Check] = []
    reject = DecisionOutcome.REJECTED
    needs = DecisionOutcome.NEEDS_EVIDENCE

    def fail(
        outcome: DecisionOutcome, name: str, detail: str
    ) -> tuple[DecisionOutcome, list[Check], Provenance | None]:
        checks.append(Check(name, False, detail))
        return outcome, checks, None

    requester = operation.requested_by
    if requester is AgentId.WARDEN:
        return fail(reject, "self_approval", "WARDEN cannot approve its own operations")
    if not policy.permits(requester, operation.kind):
        return fail(
            reject,
            "agent_permission",
            f"{requester.value} has no declared permission for {operation.kind.value}",
        )
    checks.append(Check("agent_permission", True, f"{requester.value} may {operation.kind.value}"))
    violation = policy.static_violation(operation)
    if violation is not None:
        return fail(reject, "policy_allowlist", violation)
    checks.append(Check("policy_allowlist", True, "executable, hosts and paths are allowlisted"))
    if operation.kind is not _K.CREDENTIAL_ACCESS:
        sensitive = _sensitive_reference(operation)
        if sensitive is not None:
            return fail(reject, "sensitive_path", f"operation references sensitive {sensitive!r}")
        checks.append(Check("sensitive_path", True, "no secret-bearing paths referenced"))
    if operation.kind is _K.SUBPROCESS:
        if _inline_shell(operation):
            return fail(reject, "least_privilege", "inline shell scripts cannot be reviewed")
        dangerous = _command_patterns(operation)
        if dangerous:
            return fail(reject, "dangerous_command", ", ".join(dangerous))
        checks.append(Check("command_shape", True, "no inline shell or destructive pattern"))
    if operation.kind is _K.FILE_WRITE:
        roots = (policy.workspace_dir, policy.artifact_dir)
        if not _within(operation.target, roots, policy):
            return fail(reject, "least_privilege", "writes are confined to workspace and artifacts")
        checks.append(Check("write_scope", True, "write stays in workspace/artifacts"))
    if operation.declared_requirement_id is not None:
        if requirement is None:
            return fail(needs, "declared_requirement", "declared requirement is not in the ledger")
        if requirement.status in _DEAD_REQUIREMENT_STATES:
            return fail(
                reject, "declared_requirement", f"declared requirement is {requirement.status}"
            )
    if spec is not None or operation.kind in _REQUIREMENT_GATED_KINDS:
        if requirement is None or requirement.status not in ACTIONABLE_REQUIREMENT_STATES:
            state = "missing" if requirement is None else str(requirement.status)
            return fail(
                needs,
                "declared_requirement",
                f"an approved requirement must justify this operation (requirement {state})",
            )
        checks.append(Check("declared_requirement", True, f"requirement {requirement.status}"))
    found: Provenance | None = None
    if spec is not None:
        found = provenance(operation, spec, evidence)
        missing = found.missing()
        if missing:
            checks.append(Check("provenance", False, "missing " + ", ".join(missing)))
            return needs, checks, found
        checks.append(
            Check("provenance", True, f"{found.source} {found.integrity} {found.version}")
        )
        if spec.kind == "package" and requirement is not None:
            unrelated = [name for name in spec.packages if not requirement.mentions(name)]
            if unrelated:
                checks.append(
                    Check(
                        "requirement_scope",
                        False,
                        "packages not named by the declared requirement: " + ", ".join(unrelated),
                    )
                )
                return needs, checks, found
            checks.append(Check("requirement_scope", True, "packages match the requirement"))
    return DecisionOutcome.APPROVED, checks, found


def evaluate(
    operation: Operation,
    policy: SecurityPolicy,
    *,
    requirement: RequirementFacts | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
) -> Assessment:
    """Decide deterministically. Evidence text can add risk flags but never change the outcome."""
    spec = install_spec(operation)
    flags = evidence_flags(evidence)
    outcome, checks, found = _decide(operation, policy, requirement, spec, evidence)
    risks = tuple(_risks(operation, policy, spec, flags))
    failed = [check for check in checks if not check.passed]
    if outcome is DecisionOutcome.APPROVED:
        user = policy.requires_user_authorization(operation.kind)
        version = operation.arguments.get("version")
        rationale = "all deterministic checks passed: " + "; ".join(c.name for c in checks)
        if user:
            rationale += "; explicit user authorization is still required"
        return Assessment(
            outcome=outcome,
            rationale=rationale,
            checks=tuple(checks),
            restrictions=restrictions_for(operation, policy, spec),
            risks=risks,
            version_constraint=version if isinstance(version, str) and version else None,
            needs_user_authorization=user,
            provenance=found,
        )
    reason = failed[-1] if failed else Check("unknown", False, "no rule approved the operation")
    return Assessment(
        outcome=outcome,
        rationale=f"{outcome.value}: {reason.name}: {reason.detail}",
        checks=tuple(checks),
        risks=risks,
        provenance=found,
    )
