import pytest


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime tests never construct real providers, whatever the developer's .env holds."""
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")
