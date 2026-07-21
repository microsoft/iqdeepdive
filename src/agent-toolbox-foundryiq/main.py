"""Internal HR helper that accesses Foundry IQ through a Foundry toolbox."""

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date

from agent_framework import Agent, ChatContext, ChatResponse, Message, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import FoundryToolbox, ResponsesHostServer
from agent_framework_openai import OpenAIContentFilterException
from azure.identity import AzureDeveloperCliCredential, ManagedIdentityCredential
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

logger = logging.getLogger("agent-toolbox-foundryiq")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
TOOLBOX_NAME = os.environ.get("CUSTOM_FOUNDRY_AGENT_TOOLBOX_NAME", "hr-agent-tools")
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


def main() -> None:
    """Run the toolbox-backed agent as a Responses server."""
    credential = (
        ManagedIdentityCredential()
        if "FOUNDRY_HOSTING_ENVIRONMENT" in os.environ
        else AzureDeveloperCliCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            process_timeout=60,
        )
    )
    toolbox_endpoint = (
        f"{PROJECT_ENDPOINT.rstrip('/')}/toolboxes/{TOOLBOX_NAME}/mcp?api-version=v1"
    )
    logger.info("Using Foundry toolbox MCP at %s", toolbox_endpoint)
    toolbox = FoundryToolbox(
        credential=credential,
        url=toolbox_endpoint,
        name="toolbox",
        load_prompts=False,
    )
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
        middleware=[content_filter_middleware],
    )
    agent = Agent(
        client=client,
        name="InternalHRToolboxHelper",
        instructions="""You are an internal HR helper focused on employee benefits and company information.
        Use the knowledge base tool to answer company questions and ground answers in provided context.
        Use web search only when the knowledge base does not contain the needed current information.
        Use code interpreter when calculations or structured data analysis would improve the answer.
        Use get_enrollment_deadline_info and get_current_date for benefits enrollment timing.
        If the tools do not provide enough information, say that you cannot fully answer the question.""",
        tools=[
            get_enrollment_deadline_info,
            get_current_date,
            toolbox,
        ],
        default_options={"store": False},
    )

    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    enable_instrumentation(enable_sensitive_data=True)
    main()