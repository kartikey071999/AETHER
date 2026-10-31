"""Host defaults for the runtime to merge into its immutable security policy."""

from urllib.parse import urlsplit

from aether.models.settings import ProviderSettings


def provider_hosts(settings: ProviderSettings) -> frozenset[str]:
    hosts = {urlsplit(settings.ollama_base_url.get_secret_value()).hostname}
    if settings.groq_api_key:
        hosts.add("api.groq.com")
    if settings.azure_openai_endpoint and settings.azure_openai_key and settings.openai_api_version:
        hosts.add(urlsplit(settings.azure_openai_endpoint.get_secret_value()).hostname)
    if (
        settings.anthropic_endpoint
        and settings.anthropic_api_key
        and settings.anthropic_api_version
    ):
        hosts.add(urlsplit(settings.anthropic_endpoint.get_secret_value()).hostname)
    return frozenset(host.lower().rstrip(".") for host in hosts if host)
