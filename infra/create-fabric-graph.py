"""Create or update a Fabric Graph Model over the Contoso product review tables."""

import base64
import json
import os
import time
import warnings
from pathlib import Path

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key

warnings.filterwarnings(
    "ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*"
)

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.graphmodel.models import (  # noqa: E402
    CreateGraphModelRequest,
    GraphModelPublicDefinition,
    GraphModelPublicDefinitionPart,
    UpdateGraphModelDefinitionRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"

load_dotenv(ENV_PATH, override=True)

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
LAKEHOUSE_NAME = os.getenv("LAKEHOUSE_NAME", "ContosoDIYLakehouse")
FABRIC_GRAPH_NAME = os.getenv("FABRIC_GRAPH_NAME", "ContosoDIYReviewGraph")
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com"
).rstrip("/")
FABRIC_API_URL = "https://api.fabric.microsoft.com"
REFRESH_TIMEOUT_SECONDS = 300


def require(value: str, name: str) -> str:
    """Return a required value or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to create the Fabric Graph Model.")
    return value


def definition_part(path: str, payload: dict) -> GraphModelPublicDefinitionPart:
    """Encode one public Graph Model definition part."""
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return GraphModelPublicDefinitionPart(
        path=path,
        payload=encoded,
        payload_type="InlineBase64",
    )


def build_definition(
    workspace_id: str, lakehouse_id: str
) -> GraphModelPublicDefinition:
    """Build the public definition for the product review graph."""
    table_names = (
        "products",
        "reviewers",
        "reviews",
        "features",
        "review_feature_mentions",
    )
    data_sources = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/dataSources/1.1.0/schema.json"
        ),
        "itemReferences": [
            {
                "name": "contoso_lakehouse",
                "item": {
                    "workspaceId": workspace_id,
                    "itemId": lakehouse_id,
                },
            }
        ],
        "dataSources": [
            {
                "name": f"{table_name}_table",
                "type": "DeltaTable",
                "properties": {
                    "referenceName": "contoso_lakehouse",
                    "path": f"Tables/{table_name}",
                },
            }
            for table_name in table_names
        ],
    }
    graph_type = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/graphType/1.0.0/schema.json"
        ),
        "nodeTypes": [
            {
                "alias": "product_node",
                "labels": ["Product"],
                "primaryKeyProperties": ["sku"],
                "properties": [
                    {"name": "sku", "type": "STRING"},
                    {"name": "name", "type": "STRING"},
                ],
            },
            {
                "alias": "reviewer_node",
                "labels": ["Reviewer"],
                "primaryKeyProperties": ["reviewerId"],
                "properties": [
                    {"name": "reviewerId", "type": "STRING"},
                    {"name": "displayName", "type": "STRING"},
                    {"name": "memberSince", "type": "DATETIME"},
                ],
            },
            {
                "alias": "review_node",
                "labels": ["Review"],
                "primaryKeyProperties": ["reviewId"],
                "properties": [
                    {"name": "reviewId", "type": "STRING"},
                    {"name": "rating", "type": "INT"},
                    {"name": "title", "type": "STRING"},
                    {"name": "reviewText", "type": "STRING"},
                    {"name": "reviewedAt", "type": "DATETIME"},
                    {"name": "verifiedPurchase", "type": "BOOLEAN"},
                ],
            },
            {
                "alias": "feature_node",
                "labels": ["Feature"],
                "primaryKeyProperties": ["featureId"],
                "properties": [
                    {"name": "featureId", "type": "STRING"},
                    {"name": "featureName", "type": "STRING"},
                ],
            },
        ],
        "edgeTypes": [
            {
                "alias": "has_review_edge",
                "labels": ["HAS_REVIEW"],
                "sourceNodeType": {"alias": "product_node"},
                "destinationNodeType": {"alias": "review_node"},
                "properties": [],
            },
            {
                "alias": "wrote_edge",
                "labels": ["WROTE"],
                "sourceNodeType": {"alias": "reviewer_node"},
                "destinationNodeType": {"alias": "review_node"},
                "properties": [],
            },
            {
                "alias": "mentions_edge",
                "labels": ["MENTIONS"],
                "sourceNodeType": {"alias": "review_node"},
                "destinationNodeType": {"alias": "feature_node"},
                "properties": [
                    {"name": "sentiment", "type": "STRING"},
                    {"name": "confidence", "type": "FLOAT"},
                    {"name": "evidenceExcerpt", "type": "STRING"},
                ],
            },
        ],
    }
    graph_definition = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/graphDefinition/1.0.0/schema.json"
        ),
        "nodeTables": [
            {
                "id": "product_table_mapping",
                "nodeTypeAlias": "product_node",
                "dataSourceName": "products_table",
                "propertyMappings": [
                    {"propertyName": "sku", "sourceColumn": "sku"},
                    {"propertyName": "name", "sourceColumn": "name"},
                ],
            },
            {
                "id": "reviewer_table_mapping",
                "nodeTypeAlias": "reviewer_node",
                "dataSourceName": "reviewers_table",
                "propertyMappings": [
                    {
                        "propertyName": "reviewerId",
                        "sourceColumn": "reviewer_id",
                    },
                    {
                        "propertyName": "displayName",
                        "sourceColumn": "display_name",
                    },
                    {
                        "propertyName": "memberSince",
                        "sourceColumn": "member_since",
                    },
                ],
            },
            {
                "id": "review_table_mapping",
                "nodeTypeAlias": "review_node",
                "dataSourceName": "reviews_table",
                "propertyMappings": [
                    {"propertyName": "reviewId", "sourceColumn": "review_id"},
                    {"propertyName": "rating", "sourceColumn": "rating"},
                    {"propertyName": "title", "sourceColumn": "title"},
                    {
                        "propertyName": "reviewText",
                        "sourceColumn": "review_text",
                    },
                    {
                        "propertyName": "reviewedAt",
                        "sourceColumn": "reviewed_at",
                    },
                    {
                        "propertyName": "verifiedPurchase",
                        "sourceColumn": "verified_purchase",
                    },
                ],
            },
            {
                "id": "feature_table_mapping",
                "nodeTypeAlias": "feature_node",
                "dataSourceName": "features_table",
                "propertyMappings": [
                    {
                        "propertyName": "featureId",
                        "sourceColumn": "feature_id",
                    },
                    {
                        "propertyName": "featureName",
                        "sourceColumn": "feature_name",
                    },
                ],
            },
        ],
        "edgeTables": [
            {
                "id": "has_review_table_mapping",
                "edgeTypeAlias": "has_review_edge",
                "dataSourceName": "reviews_table",
                "sourceNodeKeyColumns": ["sku"],
                "destinationNodeKeyColumns": ["review_id"],
                "propertyMappings": [],
            },
            {
                "id": "wrote_table_mapping",
                "edgeTypeAlias": "wrote_edge",
                "dataSourceName": "reviews_table",
                "sourceNodeKeyColumns": ["reviewer_id"],
                "destinationNodeKeyColumns": ["review_id"],
                "propertyMappings": [],
            },
            {
                "id": "mentions_table_mapping",
                "edgeTypeAlias": "mentions_edge",
                "dataSourceName": "review_feature_mentions_table",
                "sourceNodeKeyColumns": ["review_id"],
                "destinationNodeKeyColumns": ["feature_id"],
                "propertyMappings": [
                    {
                        "propertyName": "sentiment",
                        "sourceColumn": "sentiment",
                    },
                    {
                        "propertyName": "confidence",
                        "sourceColumn": "confidence",
                    },
                    {
                        "propertyName": "evidenceExcerpt",
                        "sourceColumn": "evidence_excerpt",
                    },
                ],
            },
        ],
    }
    positions = {
        "product_node": {"x": 40, "y": 180},
        "reviewer_node": {"x": 40, "y": 20},
        "review_node": {"x": 360, "y": 100},
        "feature_node": {"x": 680, "y": 100},
    }
    style_aliases = (*positions, "has_review_edge", "wrote_edge", "mentions_edge")
    styling = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/stylingConfiguration/1.0.0/schema.json"
        ),
        "modelLayout": {
            "positions": positions,
            "styles": {alias: {"size": 30} for alias in style_aliases},
            "pan": {"x": 0, "y": 0},
            "zoomLevel": 1,
        },
    }
    return GraphModelPublicDefinition(
        format="json",
        parts=[
            definition_part("dataSources.json", data_sources),
            definition_part("graphDefinition.json", graph_definition),
            definition_part("graphType.json", graph_type),
            definition_part("stylingConfiguration.json", styling),
        ],
    )


def find_item_by_name(items, display_name: str):
    """Return the item with the requested display name, if present."""
    return next((item for item in items if item.display_name == display_name), None)


def list_refresh_jobs(
    credential: AzureDeveloperCliCredential,
    workspace_id: str,
    graph_id: str,
) -> list[dict]:
    """List refresh jobs for a Graph Model."""
    token = credential.get_token(f"{FABRIC_API_URL}/.default").token
    response = httpx.get(
        (
            f"{FABRIC_API_URL}/v1/workspaces/{workspace_id}/GraphModels/"
            f"{graph_id}/jobs/refreshGraph/instances"
        ),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("value", [])


def is_graph_queryable(
    credential: AzureDeveloperCliCredential,
    workspace_id: str,
    graph_id: str,
) -> bool:
    """Return whether Fabric exposes a queryable graph type."""
    token = credential.get_token(f"{FABRIC_API_URL}/.default").token
    response = httpx.get(
        (
            f"{FABRIC_API_URL}/v1/workspaces/{workspace_id}/GraphModels/"
            f"{graph_id}/getQueryableGraphType?preview=true"
        ),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    return response.status_code == 200


def wait_for_new_refresh(
    credential: AzureDeveloperCliCredential,
    workspace_id: str,
    graph_id: str,
    existing_job_ids: set[str],
    active_job_ids: set[str],
) -> None:
    """Wait for the refresh initiated by a definition update."""
    started_at = time.monotonic()
    deadline = time.monotonic() + REFRESH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        relevant_jobs = [
            job
            for job in list_refresh_jobs(credential, workspace_id, graph_id)
            if job["id"] not in existing_job_ids or job["id"] in active_job_ids
        ]
        if any(job["status"] in {"Completed", "Succeeded"} for job in relevant_jobs):
            return
        failed_jobs = [job for job in relevant_jobs if job["status"] == "Failed"]
        if failed_jobs and not any(
            job["status"] in {"NotStarted", "InProgress"}
            for job in relevant_jobs
        ):
            raise RuntimeError(
                f"Fabric Graph refresh failed: {failed_jobs[0].get('failureReason')}"
            )
        if (
            not relevant_jobs
            and time.monotonic() - started_at >= 30
            and is_graph_queryable(credential, workspace_id, graph_id)
        ):
            return
        time.sleep(5)

    raise TimeoutError("Timed out waiting for the Fabric Graph refresh to complete.")


def main() -> None:
    """Create or update the review graph, refresh it, and save its identifiers."""
    tenant_id = require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        client = FabricClient(credential)
        lakehouse = find_item_by_name(
            client.lakehouse.items.list_lakehouses(workspace_id), LAKEHOUSE_NAME
        )
        if lakehouse is None:
            raise RuntimeError(
                f"Lakehouse '{LAKEHOUSE_NAME}' was not found in workspace {workspace_id}."
            )

        definition = build_definition(workspace_id, lakehouse.id)
        graph = find_item_by_name(
            client.graphmodel.items.list_graph_models(workspace_id),
            FABRIC_GRAPH_NAME,
        )
        if graph is None:
            print(f"Creating Fabric Graph Model '{FABRIC_GRAPH_NAME}'...")
            graph = client.graphmodel.items.create_graph_model(
                workspace_id,
                CreateGraphModelRequest(
                    display_name=FABRIC_GRAPH_NAME,
                    description=(
                        "Product reviews, reviewers, features, and feature-level sentiment."
                    ),
                ),
            )

        existing_jobs = list_refresh_jobs(credential, workspace_id, graph.id)
        existing_job_ids = {job["id"] for job in existing_jobs}
        active_job_ids = {
            job["id"]
            for job in existing_jobs
            if job["status"] in {"NotStarted", "InProgress"}
        }
        print(f"Saving Fabric Graph Model definition for '{FABRIC_GRAPH_NAME}'...")
        definition_poller = client.graphmodel.items.begin_update_graph_model_definition(
            workspace_id,
            graph.id,
            UpdateGraphModelDefinitionRequest(definition=definition),
        )
        definition_poller.result()

        print("Waiting for the Fabric Graph Model refresh...")
        wait_for_new_refresh(
            credential,
            workspace_id,
            graph.id,
            existing_job_ids,
            active_job_ids,
        )
    finally:
        credential.close()

    graph_ui_url = (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/graph/{graph.id}"
        "?experience=fabric-developer"
    )
    for key, value in {
        "FABRIC_GRAPH_ID": graph.id,
        "FABRIC_GRAPH_NAME": FABRIC_GRAPH_NAME,
        "FABRIC_GRAPH_UI_URL": graph_ui_url,
    }.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Fabric Graph Model: {FABRIC_GRAPH_NAME} ({graph.id})")
    print(f"Fabric UI: {graph_ui_url}")


if __name__ == "__main__":
    main()