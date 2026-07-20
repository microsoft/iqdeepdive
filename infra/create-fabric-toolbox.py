"""Create the delegated Fabric IQ connection and toolbox for the ontology agent."""

import os
import subprocess

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FabricIQPreviewToolboxTool
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

FABRIC_AUDIENCE = "https://api.fabric.microsoft.com"


def create_or_update_connection(
    endpoint: str,
    connection_name: str,
    ontology_mcp_url: str,
) -> None:
    """Create a RemoteTool connection that passes through the invoking user's identity."""
    subprocess.run(
        [
            "azd",
            "ai",
            "connection",
            "create",
            connection_name,
            "--project-endpoint",
            endpoint,
            "--kind",
            "remote-tool",
            "--target",
            ontology_mcp_url,
            "--auth-type",
            "user-entra-token",
            "--audience",
            FABRIC_AUDIENCE,
            "--force",
            "--no-prompt",
        ],
        check=True,
    )


def create_or_update_toolbox(
    endpoint: str,
    toolbox_name: str,
    connection_name: str,
    ontology_mcp_url: str,
) -> None:
    """Create a Fabric IQ toolbox version and promote it as the default version."""
    credential = AzureDeveloperCliCredential(tenant_id=os.environ["AZURE_TENANT_ID"])
    tool = FabricIQPreviewToolboxTool(
        name="fabric_iq",
        description="Query Contoso product and inventory data through the Fabric ontology.",
        project_connection_id=connection_name,
        server_label="contoso-ontology",
        server_url=ontology_mcp_url,
        require_approval="never",
    )

    print(f"Creating toolbox '{toolbox_name}' at {endpoint}...")
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    version = project.toolboxes.create_version(
        name=toolbox_name,
        tools=[tool],
        description="Fabric IQ ontology tools for Contoso product and inventory analysis.",
    )
    print(f"Created toolbox '{toolbox_name}' version {version.version}.")

    project.toolboxes.update(name=toolbox_name, default_version=version.version)
    print(f"Set toolbox '{toolbox_name}' default version to {version.version}.")


if __name__ == "__main__":
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    ontology_mcp_url = os.environ["FABRIC_ONTOLOGY_MCP_URL"]
    connection_name = os.environ.get(
        "FABRIC_IQ_CONNECTION_NAME", "fabric-ontology-connection"
    )
    toolbox_name = os.environ.get(
        "CUSTOM_FOUNDRY_FABRIC_TOOLBOX_NAME", "fabric-ontology-tools"
    )

    create_or_update_connection(
        project_endpoint,
        connection_name,
        ontology_mcp_url,
    )
    create_or_update_toolbox(
        project_endpoint,
        toolbox_name,
        connection_name,
        ontology_mcp_url,
    )