"""Read-only, redacted description and fingerprint of an effective security policy."""

import hashlib
from typing import Any

from aether.observability.redaction import redact
from aether.security.operations import canonical_json
from aether.security.policy import SecurityPolicy


def describe_policy(policy: SecurityPolicy) -> dict[str, Any]:
    """Plain-data view of ``policy`` (secret-looking values redacted); it cannot change it."""
    description = {
        "workspace_dir": str(policy.workspace_dir),
        "artifact_dir": str(policy.artifact_dir),
        "data_dir": str(policy.data_dir),
        "filesystem_roots": [str(root) for root in policy.filesystem_roots],
        "allowed_executables": sorted(policy.allowed_executables),
        "allowed_hosts": sorted(policy.allowed_hosts),
        "approval_required_kinds": sorted(kind.value for kind in policy.approval_required_kinds),
        "user_authorization_required_kinds": sorted(
            kind.value for kind in policy.user_authorization_required_kinds
        ),
        "agent_permissions": {
            agent.value: sorted(kind.value for kind in kinds)
            for agent, kinds in sorted(policy.agent_permissions.items())
        },
        "limits": {
            "max_approval_ttl_seconds": policy.max_approval_ttl.total_seconds(),
            "authorization_ttl_seconds": policy.authorization_ttl.total_seconds(),
            "subprocess_timeout_seconds": policy.subprocess_timeout_seconds,
            "max_output_bytes": policy.max_output_bytes,
            "http_timeout_seconds": policy.http_timeout_seconds,
            "max_response_bytes": policy.max_response_bytes,
            "max_redirects": policy.max_redirects,
        },
    }
    redacted: dict[str, Any] = redact(description)
    return redacted


def policy_fingerprint(policy: SecurityPolicy) -> str:
    """SHA-256 over every enforced policy field; equal policies always share a fingerprint."""
    return hashlib.sha256(canonical_json(describe_policy(policy)).encode("utf-8")).hexdigest()
