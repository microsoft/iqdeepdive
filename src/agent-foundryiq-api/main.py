"""Internal HR helper that retrieves Foundry IQ knowledge through the Search API."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from agent_framework import Agent, ChatContext, ChatResponse, Message, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import ResponsesHostServer
from agent_framework_openai import OpenAIContentFilterException
from azure.identity.aio import (
    AzureDeveloperCliCredential as AsyncAzureDeveloperCliCredential,
)
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential
from azure.search.documents.knowledgebases.aio import KnowledgeBaseRetrievalClient
from azure.search.documents.knowledgebases.models import (
    KnowledgeBaseRetrievalRequest,
    KnowledgeRetrievalIntent,
    KnowledgeRetrievalSemanticIntent,
)
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

logger = logging.getLogger("agent-foundryiq-api")


PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
SEARCH_ENDPOINT = os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"]
KNOWLEDGE_BASE_NAME = os.environ.get("AZURE_AI_SEARCH_KNOWLEDGE_BASE_NAME", "contoso-company-kb-minimal")
CONTENT_FILTER_MESSAGE = (
    "I can't help with that request because it violates content safety policies. "
    "If you have a safer or policy-compliant version of the question, I can help with that instead."
)


@tool
def get_current_date() -> str:
    """Return the current date in ISO format."""
    logger.info("Fetching current date")
    return date.today().isoformat()


@tool
def get_enrollment_deadline_info() -> dict[str, str]:
    """Return enrollment timeline details for health insurance plans."""
    logger.info("Fetching enrollment deadline information")
    return {
        "enrollment_opens": "2026-11-11",
        "enrollment_closes": "2026-11-30",
    }


async def content_filter_middleware(
    context: ChatContext, call_next: Callable[[], Awaitable[None]]
) -> None:
    """Convert model-side content-filter blocks into a friendly assistant response."""
    try:
        await call_next()
    except OpenAIContentFilterException:
        logger.info("Returning friendly refusal for content-filtered prompt")
        context.result = ChatResponse(
            messages=Message("assistant", [CONTENT_FILTER_MESSAGE]),
            finish_reason="stop",
        )


def serialize_models(models: list[Any] | None) -> list[dict[str, Any]]:
    """Convert Azure SDK response models into tool-result dictionaries."""
    return [model.as_dict() for model in models or []]


async def main() -> None:
    """Run the API-backed agent as a Responses server."""
    credential = (
        AsyncManagedIdentityCredential()
        if "FOUNDRY_HOSTING_ENVIRONMENT" in os.environ
        else AsyncAzureDeveloperCliCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            process_timeout=60,
        )
    )
    knowledge_base_client = KnowledgeBaseRetrievalClient(
        endpoint=SEARCH_ENDPOINT,
        knowledge_base_name=KNOWLEDGE_BASE_NAME,
        credential=credential,
        api_version="2026-05-01-preview",
    )

    @tool
    async def retrieve_company_knowledge(queries: list[str]) -> dict[str, Any]:
        """Retrieve grounded company and HR information from the Foundry IQ knowledge base."""
        logger.info("Retrieving company knowledge through the Azure AI Search API")
        intents: list[KnowledgeRetrievalIntent] = [KnowledgeRetrievalSemanticIntent(search=query) for query in queries]
        request = KnowledgeBaseRetrievalRequest(
            intents=intents,
            include_activity=True,
        )
        result = await knowledge_base_client.retrieve(request)
        return {
            "response": serialize_models(result.response),
            "references": serialize_models(result.references),
            "activity": serialize_models(result.activity),
        }

    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
        middleware=[content_filter_middleware],
    )
    agent = Agent(
        client=client,
        name="InternalHRApiHelper",
        instructions="""You are an internal HR helper focused on employee benefits and company information.
        Call retrieve_company_knowledge to answer company or benefits questions, and ground answers in its response.
        Cite the reference IDs supplied by the tool when available.
        Use get_enrollment_deadline_info and get_current_date for benefits enrollment timing.
        If the tools do not provide enough information, say that you cannot fully answer the question.""",
        tools=[
            retrieve_company_knowledge,
            get_enrollment_deadline_info,
            get_current_date,
        ],
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    try:
        await server.run_async()
    finally:
        await knowledge_base_client.close()
        await credential.close()


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    enable_instrumentation(enable_sensitive_data=True)
    asyncio.run(main())
