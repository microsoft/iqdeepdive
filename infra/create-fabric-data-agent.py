"""Create and publish a Fabric data agent backed by Contoso ontology and Graph."""

import os
import time
from pathlib import Path

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
FABRIC_API_URL = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = f"{FABRIC_API_URL}/.default"
OPERATION_TIMEOUT_SECONDS = 300
AI_INSTRUCTIONS = """Use the ontology for Contoso DIY product catalog, category, supplier,
store, and current inventory facts. Use the review graph for reviewers, reviews, products,
product features, feature-level sentiment, and relationship traversals. Join results across
sources by product SKU when a question requires both operational and customer-review context.
"""
ONTOLOGY_DESCRIPTION = (
    "Contoso DIY product catalog, categories, suppliers, stores, and current inventory."
)
ONTOLOGY_INSTRUCTIONS = """Generate Fabric Ontology GQL. Property names are case-sensitive.
When sorting a projected expression, use its RETURN alias exactly; never invent a different
capitalization. Inventory uses quantityOnHand and availableQuantity. Product uses sku and name.
Store HOLDS Inventory, and Inventory RECORDS_STOCK_FOR Product."""
GRAPH_DESCRIPTION = (
    "Contoso DIY product reviews, reviewers, product features, feature-level sentiment, "
    "and relationships between products, reviews, reviewers, and features."
)
GRAPH_INSTRUCTIONS = """Generate Fabric Graph GQL, not Cypher. Use FILTER after MATCH for
predicates; never use WHERE. Support group by in GQL. The graph has Product, Reviewer, Review,
and Feature nodes, with HAS_REVIEW, WROTE, and MENTIONS edges. MENTIONS has sentiment,
confidence, and evidenceExcerpt properties. Product and Feature use sku and featureName,
respectively. Review uses reviewId, rating, reviewText, and reviewDate."""
GRAPH_FEWSHOTS = {
    "Which product features are mentioned most often in reviews?": (
        "MATCH (review:`Review`)-[mentions:`MENTIONS`]->(feature:`Feature`)\n"
        "LET featureName = feature.`featureName`\n"
        "RETURN featureName, COUNT(mentions) AS mentionCount\n"
        "GROUP BY featureName\n"
        "ORDER BY mentionCount DESC"
    ),
    "Which product features have the most negative mentions?": (
        "MATCH (review:`Review`)-[mentions:`MENTIONS`]->(feature:`Feature`)\n"
        "FILTER mentions.`sentiment` = 'negative'\n"
        "LET featureName = feature.`featureName`\n"
        "RETURN featureName, COUNT(mentions) AS negativeMentionCount\n"
        "GROUP BY featureName\n"
        "ORDER BY negativeMentionCount DESC"
    ),
}

load_dotenv(ENV_PATH, override=True)


def require_env(name: str) -> str:
    """Return a required environment variable or fail with a useful message."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required to create the Fabric data agent.")
    return value


def get_fabric_token(tenant_id: str) -> str:
    """Acquire a Fabric API token and close the credential immediately."""
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        return credential.get_token(FABRIC_SCOPE).token
    finally:
        credential.close()


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected_statuses: set[int],
    json: dict | None = None,
) -> httpx.Response:
    """Send a Fabric request and require one of the expected status codes."""
    response = client.request(method, url, json=json)
    if response.status_code not in expected_statuses:
        response.raise_for_status()
        raise RuntimeError(
            f"Unexpected Fabric API status {response.status_code} for {method} {url}."
        )
    return response


def wait_for_operation(
    client: httpx.Client,
    response: httpx.Response,
    *,
    ignored_error_codes: set[str] | None = None,
) -> None:
    """Wait for a Fabric long-running operation when the API returns 202."""
    if response.status_code != httpx.codes.ACCEPTED:
        return

    operation_url = response.headers.get("Location")
    if not operation_url:
        raise RuntimeError("Fabric returned 202 without an operation Location header.")

    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        operation = request(
            client,
            "GET",
            operation_url,
            expected_statuses={httpx.codes.OK},
        )
        payload = operation.json()
        status = payload.get("status")
        if status == "Succeeded":
            return
        if status in {"Failed", "Cancelled"}:
            error_code = payload.get("error", {}).get("errorCode")
            if error_code in (ignored_error_codes or set()):
                return
            raise RuntimeError(f"Fabric operation {status.lower()}: {payload}")

        retry_after = float(operation.headers.get("Retry-After", "2"))
        time.sleep(min(max(retry_after, 1), 10))

    raise TimeoutError("Timed out waiting for the Fabric operation to complete.")


def list_data_agents(client: httpx.Client, workspace_id: str) -> list[dict]:
    """List data agents in the configured workspace."""
    response = request(
        client,
        "GET",
        f"/v1/workspaces/{workspace_id}/dataAgents",
        expected_statuses={httpx.codes.OK},
    )
    return response.json().get("value", [])


def get_or_create_data_agent(
    client: httpx.Client,
    name: str,
    workspace_id: str,
) -> dict:
    """Return an existing data agent by name or create it with the Fabric API."""
    for item in list_data_agents(client, workspace_id):
        if item.get("displayName") == name:
            print(f"Reusing Fabric data agent '{name}'.")
            return item

    response = request(
        client,
        "POST",
        f"/v1/workspaces/{workspace_id}/dataAgents",
        expected_statuses={httpx.codes.OK, httpx.codes.CREATED, httpx.codes.ACCEPTED},
        json={"artifactType": "LLMPlugin", "displayName": name},
    )
    wait_for_operation(client, response)

    for _ in range(30):
        for item in list_data_agents(client, workspace_id):
            if item.get("displayName") == name:
                return item
        time.sleep(2)

    raise RuntimeError(f"Fabric data agent '{name}' was created but could not be found.")


def list_staging_datasources(client: httpx.Client, base_url: str) -> list[dict]:
    """List data sources in the data agent's staging configuration."""
    response = request(
        client,
        "GET",
        f"{base_url}/staging/datasources",
        expected_statuses={httpx.codes.OK},
    )
    return response.json().get("value", [])


def add_fabric_item_datasource(
    client: httpx.Client,
    base_url: str,
    workspace_id: str,
    item_id: str,
    display_name: str,
) -> None:
    """Add a Fabric item to staging unless that exact item is already present."""
    existing_item_ids = {
        source.get("itemReference", {}).get("itemId")
        for source in list_staging_datasources(client, base_url)
    }
    if item_id in existing_item_ids:
        print(f"Reusing {display_name} data source {item_id}.")
        return

    print(f"Adding {display_name} {item_id} as a data source...")
    response = request(
        client,
        "POST",
        f"{base_url}/staging/datasources",
        expected_statuses={
            httpx.codes.OK,
            httpx.codes.CREATED,
            httpx.codes.ACCEPTED,
        },
        json={
            "type": "FabricItem",
            "itemReference": {
                "referenceType": "ById",
                "itemId": item_id,
                "workspaceId": workspace_id,
            },
        },
    )
    wait_for_operation(client, response)


def get_staging_datasource(
    client: httpx.Client,
    base_url: str,
    item_id: str,
) -> dict:
    """Return the staging data source associated with a Fabric item."""
    for source in list_staging_datasources(client, base_url):
        if source.get("itemReference", {}).get("itemId") == item_id:
            return source
    raise RuntimeError(f"Fabric item {item_id} is not a staging data source.")


def configure_datasource(
    client: httpx.Client,
    base_url: str,
    item_id: str,
    source_name: str,
    description: str,
    instructions: str,
    fewshots: dict[str, str],
) -> None:
    """Configure source-specific generation guidance and few-shot examples."""
    datasource = get_staging_datasource(client, base_url, item_id)
    datasource_id = datasource["id"]
    response = request(
        client,
        "PATCH",
        f"{base_url}/staging/datasources/{datasource_id}",
        expected_statuses={httpx.codes.OK},
        json={
            "description": description,
            "instructions": instructions,
        },
    )
    wait_for_operation(client, response)

    if not fewshots:
        return

    fewshots_response = request(
        client,
        "GET",
        f"{base_url}/staging/datasources/{datasource_id}/fewshots",
        expected_statuses={httpx.codes.OK},
    )
    existing_by_question = {
        example["question"]: example
        for example in fewshots_response.json().get("value", [])
    }
    for question, query in fewshots.items():
        existing = existing_by_question.get(question)
        if existing is None:
            print(f"Adding {source_name} example query: {question}")
            request(
                client,
                "POST",
                f"{base_url}/staging/datasources/{datasource_id}/fewshots",
                expected_statuses={httpx.codes.CREATED},
                json={"question": question, "query": query},
            )
        elif existing.get("query") != query:
            print(f"Updating {source_name} example query: {question}")
            request(
                client,
                "PATCH",
                f"{base_url}/staging/datasources/{datasource_id}/fewshots/{existing['id']}",
                expected_statuses={httpx.codes.OK},
                json={"query": query},
            )
        else:
            print(f"Reusing {source_name} example query: {question}")


def main() -> None:
    """Create or update the ontology and Graph-backed data agent and publish it."""
    tenant_id = require_env("FABRIC_TENANT_ID")
    workspace_id = require_env("FABRIC_WORKSPACE_ID")
    ontology_id = require_env("FABRIC_ONTOLOGY_ID")
    graph_id = require_env("FABRIC_GRAPH_ID")
    data_agent_name = os.getenv("FABRIC_DATA_AGENT_NAME", "ContosoDIYDataAgent")

    token = get_fabric_token(tenant_id)
    with httpx.Client(
        base_url=FABRIC_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as client:
        print(f"Creating or reusing Fabric data agent '{data_agent_name}'...")
        data_agent = get_or_create_data_agent(client, data_agent_name, workspace_id)
        data_agent_id = data_agent["id"]
        base_url = f"/v1/workspaces/{workspace_id}/dataAgents/{data_agent_id}"
        settings_response = request(
            client,
            "PATCH",
            f"{base_url}/staging/settings",
            expected_statuses={httpx.codes.OK, httpx.codes.ACCEPTED},
            json={"aiInstructions": AI_INSTRUCTIONS},
        )
        wait_for_operation(client, settings_response)

        add_fabric_item_datasource(
            client,
            base_url,
            workspace_id,
            ontology_id,
            "ontology",
        )
        add_fabric_item_datasource(
            client,
            base_url,
            workspace_id,
            graph_id,
            "review graph",
        )
        configure_datasource(
            client,
            base_url,
            ontology_id,
            "ontology",
            ONTOLOGY_DESCRIPTION,
            ONTOLOGY_INSTRUCTIONS,
            {},
        )
        configure_datasource(
            client,
            base_url,
            graph_id,
            "Graph",
            GRAPH_DESCRIPTION,
            GRAPH_INSTRUCTIONS,
            GRAPH_FEWSHOTS,
        )

        print("Publishing the Fabric data agent...")
        publish_response = request(
            client,
            "POST",
            f"{base_url}/staging/publish",
            expected_statuses={httpx.codes.OK, httpx.codes.CREATED, httpx.codes.ACCEPTED},
            json={
                "publishedDescription": (
                    "Contoso DIY product operations and review intelligence data agent"
                )
            },
        )
        wait_for_operation(client, publish_response)

    mcp_url = (
        f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{workspace_id}"
        f"/dataagents/{data_agent_id}/agent"
    )
    values = {
        "FABRIC_DATA_AGENT_ID": data_agent_id,
        "FABRIC_DATA_AGENT_MCP_URL": mcp_url,
        "FABRIC_DATA_AGENT_NAME": data_agent_name,
    }
    ENV_PATH.touch()
    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Published Fabric data agent: {data_agent_id}")
    print(f"MCP endpoint: {mcp_url}")


if __name__ == "__main__":
    main()