"""Startup configuration for the web research tools (``AETHER_WEB_*``, JSON lists)."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether import __version__

DEFAULT_WEB_ALLOWED_HOSTS = (
    "pypi.org",
    "api.github.com",
    "github.com",
    "docs.python.org",
    "readthedocs.io",
    "*.readthedocs.io",
)
DEFAULT_DOCS_HOSTS = ("docs.python.org", "*.readthedocs.io")
DEFAULT_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "text/xml",
    "application/xml",
    "application/json",
)
DEFAULT_USER_AGENT = (
    f"AETHER-research/{__version__} (+local research agent; read-only; respects robots.txt)"
)


class WebSettings(BaseSettings):
    """Web research limits and politeness. Hosts are frozen into the security policy at startup."""

    model_config = SettingsConfigDict(
        env_prefix="AETHER_WEB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A malformed token must never be echoed back in a validation error.
        hide_input_in_errors=True,
    )

    allowed_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_WEB_ALLOWED_HOSTS))
    # Hosts docs.search may query; each must also be on allowed_hosts to be reachable.
    docs_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_DOCS_HOSTS))
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=8, max_length=256)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_page_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    allowed_content_types: list[str] = Field(default_factory=lambda: list(DEFAULT_CONTENT_TYPES))
    cache_ttl_seconds: int = Field(default=6 * 3600, ge=0, le=30 * 24 * 3600)
    robots_ttl_seconds: int = Field(default=24 * 3600, ge=60, le=30 * 24 * 3600)
    per_host_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    max_crawl_pages: int = Field(default=10, ge=1, le=100)
    max_crawl_depth: int = Field(default=2, ge=0, le=5)
    max_extract_chars: int = Field(default=20_000, ge=256, le=1_000_000)
    max_links: int = Field(default=200, ge=1, le=5000)
    max_search_results: int = Field(default=10, ge=1, le=50)
    # Optional fine-grained, public-read-only token; only ever sent to api.github.com.
    github_token: SecretStr | None = Field(default=None, repr=False)

    @field_validator("allowed_content_types")
    @classmethod
    def _normalize_types(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in value if item.strip()})

    @field_validator("allowed_hosts", "docs_hosts")
    @classmethod
    def _normalize_hosts(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().rstrip(".").lower() for item in value if item.strip()})

    @field_validator("github_token")
    @classmethod
    def _clean_token(cls, value: SecretStr | None) -> SecretStr | None:
        token = value.get_secret_value().strip() if value is not None else ""
        if not token:
            return None
        if not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
            raise ValueError("github_token must be a single printable ASCII token")
        return SecretStr(token)

    @field_validator("user_agent")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if any(char in value for char in "\r\n"):
            raise ValueError("user_agent must be a single line")
        return value.strip()
