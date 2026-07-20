"""Workplace assistant that accesses Microsoft 365 context through Work IQ."""

import logging
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import FoundryToolbox, ResponsesHostServer
from azure.identity import (
    AzureDeveloperCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
)
from dotenv import load_dotenv
from workiq_consent import enable_work_iq_consent_handling

load_dotenv(dotenv_path=".env", override=True)

logger = logging.getLogger("agent-foundry-iq-workiq-toolbox")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
TOOLBOX_NAME = os.environ.get(
    "CUSTOM_FOUNDRY_WORKIQ_TOOLBOX_NAME", "work-iq-tools"
)


def main() -> None:
    """Run the Work IQ toolbox-backed agent as a Responses server."""
    enable_work_iq_consent_handling()
    tenant_id = os.getenv("AZURE_TENANT_ID")
    azure_developer_cli_credential = (
        AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
        if tenant_id
        else AzureDeveloperCliCredential(process_timeout=60)
    )
    credential = ChainedTokenCredential(
        ManagedIdentityCredential(),
        azure_developer_cli_credential,
    )
    toolbox_endpoint = (
        f"{PROJECT_ENDPOINT.rstrip('/')}/toolboxes/{TOOLBOX_NAME}/mcp?api-version=v1"
    )
    logger.info("Using Foundry toolbox MCP at %s", toolbox_endpoint)
    toolbox = FoundryToolbox(
        credential=credential,
        url=toolbox_endpoint,
        name="work_iq_toolbox",
        load_prompts=False,
    )
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
    )
    agent = Agent(
        client=client,
        name="Microsoft365WorkplaceAssistant",
        instructions="""You are a workplace assistant grounded in the signed-in user's Microsoft 365 context.
        Use Work IQ for every question about the user's email, chats, meetings, colleagues, or documents.
        Respect the returned permissions and sensitivity boundaries. Distinguish sourced facts from inference,
        mention relevant dates and participants, and say when Work IQ does not provide enough information.
        Never claim access to content that the signed-in user cannot access.""",
        tools=[toolbox],
        default_options={"store": False},
    )

    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    enable_instrumentation(enable_sensitive_data=True)
    main()