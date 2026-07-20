"""Inventory analyst that accesses a Fabric ontology through a Foundry toolbox."""

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

load_dotenv(dotenv_path=".env", override=True)

logger = logging.getLogger("agent-foundry-iq-fabric-toolbox")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
TOOLBOX_NAME = os.environ.get(
    "CUSTOM_FOUNDRY_FABRIC_TOOLBOX_NAME", "fabric-ontology-tools"
)


def main() -> None:
    """Run the Fabric IQ toolbox-backed agent as a Responses server."""
    credential = ChainedTokenCredential(
        ManagedIdentityCredential(),
        AzureDeveloperCliCredential(
            tenant_id=os.getenv("AZURE_TENANT_ID"),
            process_timeout=60,
        ),
    )
    toolbox_endpoint = (
        f"{PROJECT_ENDPOINT.rstrip('/')}/toolboxes/{TOOLBOX_NAME}/mcp?api-version=v1"
    )
    logger.info("Using Foundry toolbox MCP at %s", toolbox_endpoint)
    toolbox = FoundryToolbox(
        credential=credential,
        url=toolbox_endpoint,
        name="fabric_iq_toolbox",
        load_prompts=False,
    )
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
    )
    agent = Agent(
        client=client,
        name="ContosoFabricInventoryAnalyst",
        instructions="""You are a Contoso inventory and product analyst.
        Use the Fabric IQ ontology tool for every question about products, categories, inventory,
        stock levels, suppliers, or related business data. Base answers only on data returned by
        the ontology. Clearly state when the ontology does not contain enough information.
        Summarize results concisely and include relevant product names, categories, and quantities.""",
        tools=[toolbox],
        default_options={"store": False},
    )

    ResponsesHostServer(agent).run()


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    enable_instrumentation(enable_sensitive_data=True)
    main()