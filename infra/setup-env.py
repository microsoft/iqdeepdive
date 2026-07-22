"""Write local agent and notebook settings to the repository root .env file."""

import os
from pathlib import Path

import requests
from azure.identity import AzureDeveloperCliCredential
from dotenv import set_key

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"


def post(url: str, headers: dict[str, str]) -> dict:
    """POST to an Azure management endpoint and return its JSON response."""
    response = requests.post(url, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


def main() -> None:
    """Fetch local-auth keys and write the complete local development environment."""
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    resource_group = os.environ["AZURE_RESOURCE_GROUP"]
    tenant_id = os.environ["AZURE_TENANT_ID"]
    search_name = os.environ["AZURE_AI_SEARCH_SERVICE_NAME"]
    openai_name = os.environ["AZURE_AI_ACCOUNT_NAME"]

    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    token = credential.get_token(MANAGEMENT_SCOPE).token
    headers = {"Authorization": f"Bearer {token}"}

    search_key = post(
        f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Search/searchServices/{search_name}"
        f"/listAdminKeys?api-version=2023-11-01",
        headers,
    )["primaryKey"]
    openai_key = post(
        f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{openai_name}"
        f"/listKeys?api-version=2023-05-01",
        headers,
    )["key1"]

    ENV_PATH.touch()
    values = {
        "AZURE_TENANT_ID": tenant_id,
        "AZURE_SUBSCRIPTION_ID": subscription_id,
        "AZURE_RESOURCE_GROUP": resource_group,
        "AZURE_LOCATION": os.environ["AZURE_LOCATION"],
        "FOUNDRY_PROJECT_ENDPOINT": os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        "AZURE_AI_PROJECT_ENDPOINT": os.environ["AZURE_AI_PROJECT_ENDPOINT"],
        "AZURE_AI_PROJECT_ID": os.environ["AZURE_AI_PROJECT_ID"],
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        "AZURE_OPENAI_ENDPOINT": os.environ["AZURE_OPENAI_ENDPOINT"],
        "AZURE_OPENAI_KEY": openai_key,
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": os.environ["AZURE_OPENAI_CHATGPT_DEPLOYMENT"],
        "AZURE_OPENAI_CHATGPT_MODEL_NAME": os.environ["AZURE_OPENAI_CHATGPT_MODEL_NAME"],
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"],
        "AZURE_AI_SEARCH_SERVICE_ENDPOINT": os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"],
        "AZURE_AI_SEARCH_SERVICE_NAME": search_name,
        "AZURE_SEARCH_SERVICE_ENDPOINT": os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"],
        "AZURE_SEARCH_SERVICE_NAME": search_name,
        "AZURE_SEARCH_ADMIN_KEY": search_key,
        "AZURE_AI_SEARCH_KNOWLEDGE_BASE_NAME": "contoso-company-kb-minimal",
        "AZURE_AI_SEARCH_KB_MCP_CONNECTION_NAME": "kb-mcp-connection",
        "CUSTOM_FOUNDRY_AGENT_TOOLBOX_NAME": "hr-agent-tools",
        "AZURE_AI_SEARCH_WORKIQ_KNOWLEDGE_BASE_NAME": "multisource-workiq-knowledge-base",
        "AZURE_AI_SEARCH_WORKIQ_KB_MCP_CONNECTION_NAME": "workiq-kb-mcp-connection",
        "CUSTOM_FOUNDRY_WORKIQ_KB_TOOLBOX_NAME": "workiq-knowledge-tools",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": os.environ.get(
            "APPLICATIONINSIGHTS_CONNECTION_STRING", ""
        ),
        "APPLICATIONINSIGHTS_RESOURCE_ID": os.environ.get("APPLICATIONINSIGHTS_RESOURCE_ID", ""),
        "FABRIC_CAPACITY_ID": os.environ.get("FABRIC_CAPACITY_ID", ""),
    }
    fabric_tenant_id = os.environ.get("FABRIC_TENANT_ID", "")
    if fabric_tenant_id:
        values["FABRIC_TENANT_ID"] = fabric_tenant_id

    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Created {ENV_PATH}")


if __name__ == "__main__":
    main()
