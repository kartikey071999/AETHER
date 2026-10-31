"""API dependencies and safe serialization at the HTTP boundary."""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from aether.agents.base import AgentContext
from aether.api.queries import ApiRepository
from aether.api.schemas import DEFAULT_LIMIT, MAX_LIMIT
from aether.communication.bus import MessageBus
from aether.config import AetherSettings
from aether.models.settings import ProviderSettings
from aether.observability.recorder import EventRecorder
from aether.observability.redaction import REDACTED, is_secret_key, redact_string
from aether.runtime.control import TaskControl
from aether.runtime.security import build_security_policy
from aether.security.permissions import PermissionService
from aether.security.readonly import PermissionView
from aether.storage.database import Database

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]
_bearer = HTTPBearer(auto_error=False)
_REFERENCE_KEYS = frozenset(
    {"authorization_request_id", "authorization_required", "authorization_expires_at"}
)


class ApiServices:
    def __init__(
        self,
        settings: AetherSettings,
        database: Database,
        provider_settings: ProviderSettings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.provider_settings = provider_settings
        self.repository = ApiRepository(database.sessions, clock=clock)
        self.bus = MessageBus(database.sessions, clock=clock)
        self.control = TaskControl(database.sessions, actor="API", clock=clock)
        self.clock = clock
        self._muse: AgentContext | None = None
        values = [settings.api_token, settings.database_url]
        values.extend(
            value
            for name in type(provider_settings).model_fields
            if isinstance(value := getattr(provider_settings, name), SecretStr)
        )
        self._secret_values = tuple(
            sorted(
                {
                    value.get_secret_value()
                    for value in values
                    if value is not None and value.get_secret_value()
                }
                | {make_url(settings.database_url.get_secret_value()).password or ""},
                key=len,
                reverse=True,
            )
        )

    def muse_context(self) -> AgentContext:
        if self._muse is None:
            permissions = PermissionService(
                self.database.sessions,
                EventRecorder(),
                build_security_policy(self.settings),
                clock=self.clock,
            )
            self._muse = AgentContext(
                sessions=self.database.sessions,
                bus=self.bus,
                clock=self.clock,
                permission_service=PermissionView(permissions),
            )
        return self._muse

    def sanitize(self, value: Any) -> Any:
        value = jsonable_encoder(
            value,
            custom_encoder={
                datetime: lambda stamp: (
                    stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
                ).isoformat()
            },
        )

        def scrub(item: Any) -> Any:
            if isinstance(item, str):
                for secret in self._secret_values:
                    if secret:
                        item = item.replace(secret, REDACTED)
                return redact_string(item)
            if isinstance(item, dict):
                return {
                    scrub(key): (
                        REDACTED
                        if is_secret_key(key) and key not in _REFERENCE_KEYS
                        else scrub(child)
                    )
                    for key, child in item.items()
                }
            if isinstance(item, list):
                return [scrub(child) for child in item]
            return item

        return scrub(value)

    def response(self, value: Any, *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            self.sanitize(value),
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )


def services(request: Request) -> ApiServices:
    return request.app.state.services


Services = Annotated[ApiServices, Depends(services)]


def pagination(limit: Limit = DEFAULT_LIMIT, offset: Offset = 0) -> dict[str, int]:
    return {"limit": limit, "offset": offset}


Pagination = Annotated[dict[str, int], Depends(pagination)]


def require_admin(
    api: Services,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    token = api.settings.api_token
    if token is None or not token.get_secret_value():
        raise HTTPException(503, "Administrative endpoints are disabled")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode("utf-8"), token.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            401, "A valid bearer token is required", headers={"WWW-Authenticate": "Bearer"}
        )
