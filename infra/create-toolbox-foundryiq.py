"""Create or update a Foundry toolbox backed by a Search knowledge base."""

import argparse
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    CodeInterpreterToolboxTool,
    MCPToolboxTool,
    ToolboxTool,
    WebSearchToolboxTool,
)
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)


def parse_args() -> argparse.Namespace:
    """Parse optional toolbox and knowledge-base overrides."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolbox-name")
    parser.add_argument("--knowledge-base-name")
    parser.add_argument("--connection-name")
    parser.add_argument(
        "--knowledge-base-description",
        default="Retrieve grounded company and HR information.",
    )
    parser.add_argument(
        "--toolbox-description",
        default="Web search, code interpreter, and company knowledge tools for the HR agent.",
    )
    return parser.parse_args()


def create_or_update_toolbox(
    endpoint: str,
    toolbox_name: str,
    knowledge_base_mcp_url: str,
    search_connection_name: str,
    knowledge_base_description: str,
    toolbox_description: str,
) -> None:
    """Create a toolbox version and promote it as the default version."""
    credential = AzureDeveloperCliCredential(tenant_id=os.environ["AZURE_TENANT_ID"])
    tools: list[ToolboxTool] = [
        WebSearchToolboxTool(
            name="web_search",
            description="Search the public web for current information.",
            search_context_size="medium",
        ),
        CodeInterpreterToolboxTool(
            name="code_interpreter",
            description="Run Python in a sandbox for calculations and structured data analysis.",
        ),
        MCPToolboxTool(
            server_label="knowledge-base",
            server_url=knowledge_base_mcp_url,
            server_description=knowledge_base_description,
            project_connection_id=search_connection_name,
            allowed_tools=["knowledge_base_retrieve"],
            require_approval="never",
        ),
    ]

    print(f"Creating toolbox '{toolbox_name}' at {endpoint}...")
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    version = project.toolboxes.create_version(
        name=toolbox_name,
        tools=tools,
        description=toolbox_description,
    )
    print(f"Created toolbox '{toolbox_name}' version {version.version}.")

    project.toolboxes.update(name=toolbox_name, default_version=version.version)
    print(f"Set toolbox '{toolbox_name}' default version to {version.version}.")


if __name__ == "__main__":
    args = parse_args()
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    search_endpoint = os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"]
    knowledge_base_name = args.knowledge_base_name or os.environ.get(
        "AZURE_AI_SEARCH_KNOWLEDGE_BASE_NAME", "contoso-company-kb-minimal"
    )
    toolbox_name = args.toolbox_name or os.environ.get(
        "CUSTOM_FOUNDRY_AGENT_TOOLBOX_NAME", "hr-agent-tools"
    )
    search_connection_name = args.connection_name or os.environ.get(
        "AZURE_AI_SEARCH_KB_MCP_CONNECTION_NAME", "kb-mcp-connection"
    )
    knowledge_base_mcp_url = (
        f"{search_endpoint.rstrip('/')}/knowledgebases/{knowledge_base_name}"
        "/mcp?api-version=2026-05-01-preview"
    )

    create_or_update_toolbox(
        project_endpoint,
        toolbox_name,
        knowledge_base_mcp_url,
        search_connection_name,
        args.knowledge_base_description,
        args.toolbox_description,
    )
