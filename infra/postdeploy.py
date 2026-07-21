"""Grant Azure AI Search access to deployed hosted-agent identities."""

import json
import os
import subprocess
import sys
import uuid

from azure.core.exceptions import HttpResponseError
from azure.identity import AzureDeveloperCliCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

SEARCH_DATA_CONTRIBUTOR_ROLE_ID = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
AGENT_NAMES = (
    "agent-foundryiq-mcp",
    "agent-foundryiq-api",
    "agent-toolbox-foundryiq",
)
FOUNDRY_USER_AGENT_NAMES = (
    "agent-toolbox-foundryiq-workiq",
    "agent-toolbox-workiq",
)


def get_agent_principal_id(agent_name: str) -> str | None:
    """Return a hosted agent instance identity, or None if it is not deployed."""
    result = subprocess.run(
        ["azd", "ai", "agent", "show", agent_name, "--output", "json", "--no-prompt"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"{agent_name} is not deployed. Skipping role assignment.")
        return None

    agent = json.loads(result.stdout)
    principal_id = agent.get("instance_identity", {}).get("principal_id")
    if not principal_id:
        raise RuntimeError(f"Could not retrieve the hosted identity for {agent_name}.")
    return principal_id


def assign_search_access(client: AuthorizationManagementClient, scope: str, agent_name: str, principal_id: str):
    """Create the Search data role assignment, or reuse it when already present."""
    role_definition_id = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{SEARCH_DATA_CONTRIBUTOR_ROLE_ID}"
    assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{principal_id}:{SEARCH_DATA_CONTRIBUTOR_ROLE_ID}"))
    parameters = RoleAssignmentCreateParameters(
        principal_id=principal_id,
        principal_type="ServicePrincipal",
        role_definition_id=role_definition_id,
    )

    print(f"Assigning Search Index Data Contributor to {agent_name} ({principal_id})...")
    try:
        client.role_assignments.create(scope, assignment_name, parameters)
    except HttpResponseError as error:
        if error.status_code != 409:
            raise
        print(f"Search access already assigned to {agent_name}.")


def assign_foundry_user(
    client: AuthorizationManagementClient,
    subscription_id: str,
    scope: str,
    agent_name: str,
    principal_id: str,
) -> None:
    """Assign Foundry User at one scope, or reuse the existing assignment."""
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{FOUNDRY_USER_ROLE_ID}"
    )
    assignment_name = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{scope}:{principal_id}:{FOUNDRY_USER_ROLE_ID}",
        )
    )
    parameters = RoleAssignmentCreateParameters(
        principal_id=principal_id,
        principal_type="ServicePrincipal",
        role_definition_id=role_definition_id,
    )

    print(f"Assigning Foundry User to {agent_name} at {scope}...")
    try:
        client.role_assignments.create(scope, assignment_name, parameters)
    except HttpResponseError as error:
        if error.status_code != 409:
            raise
        print(f"Foundry User already assigned at {scope}.")


def main() -> int:
    """Grant Search access to each deployed hosted agent."""
    print("Assigning Search access to the hosted agent identity...")

    service_name = os.getenv("AZURE_AI_SEARCH_SERVICE_NAME")
    subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    resource_group = os.getenv("AZURE_RESOURCE_GROUP")
    if not service_name or not subscription_id or not resource_group:
        print("Search service or subscription information is not set. Skipping role assignment.")
        return 0

    scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Search/searchServices/{service_name}"
    )
    credential = AzureDeveloperCliCredential(tenant_id=os.getenv("AZURE_TENANT_ID", ""))
    client = AuthorizationManagementClient(credential, subscription_id)

    for agent_name in AGENT_NAMES:
        principal_id = get_agent_principal_id(agent_name)
        if principal_id:
            assign_search_access(client, scope, agent_name, principal_id)

    project_id = os.getenv("AZURE_AI_PROJECT_ID")
    if project_id:
        account_scope = project_id.rsplit("/projects/", 1)[0]
        for agent_name in FOUNDRY_USER_AGENT_NAMES:
            principal_id = get_agent_principal_id(agent_name)
            if principal_id:
                for foundry_scope in (account_scope, project_id):
                    assign_foundry_user(
                        client,
                        subscription_id,
                        foundry_scope,
                        agent_name,
                        principal_id,
                    )

    print("Postdeploy setup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())