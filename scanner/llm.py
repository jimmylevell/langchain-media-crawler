"""Chat model on Azure OpenAI / Microsoft Foundry.

Two ways in, chosen by environment:
- AZURE_OPENAI_ENDPOINT set  → Azure OpenAI endpoint, API key or Entra ID (local, App Service).
- only FOUNDRY_PROJECT_ENDPOINT → Foundry project endpoint with the agent's identity
  (this is what a Foundry hosted agent container gets injected).
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel

from . import config  # noqa: F401  (loads .env)

MAX_RETRIES = 8  # parallel agents hit 429s on small deployments; back off instead of failing
TIMEOUT = 180


def make_llm() -> BaseChatModel:
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    project = os.getenv("FOUNDRY_PROJECT_ENDPOINT")
    if not deployment or not (endpoint or project):
        raise SystemExit(
            "AZURE_OPENAI_ENDPOINT und AZURE_OPENAI_DEPLOYMENT fehlen. "
            "Kopieren Sie .env.example nach .env und tragen Sie die Werte ein."
        )
    if endpoint:
        return _azure_openai(endpoint, deployment)
    return _foundry_project(project, deployment)


def _azure_openai(endpoint: str, deployment: str) -> BaseChatModel:
    from langchain_openai import AzureChatOpenAI

    # Accept a pasted Foundry project URL (…/api/projects/<name>) too: only the host is needed.
    parts = urlsplit(endpoint)
    kwargs: dict = dict(
        azure_endpoint=f"{parts.scheme}://{parts.netloc}/",
        azure_deployment=deployment,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
        max_retries=MAX_RETRIES,
        timeout=TIMEOUT,
    )
    if not os.getenv("AZURE_OPENAI_API_KEY"):
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
    return AzureChatOpenAI(**kwargs)


def _foundry_project(project_endpoint: str, deployment: str) -> BaseChatModel:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import ChatOpenAI

    credential = DefaultAzureCredential()
    openai_client = AIProjectClient(endpoint=project_endpoint.rstrip("/"), credential=credential).get_openai_client()
    return ChatOpenAI(
        model=deployment,
        base_url=str(openai_client.base_url),
        api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
        use_responses_api=True,
        max_retries=MAX_RETRIES,
        timeout=TIMEOUT,
    )
