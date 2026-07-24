"""Create or update the Direct Lake web analytics semantic model."""

import base64
import os
import time
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key
from microsoft_fabric_api import FabricClient
from microsoft_fabric_api.generated.semanticmodel.models import (
    CreateSemanticModelRequest,
    SemanticModelDefinition,
    SemanticModelDefinitionPart,
    UpdateSemanticModelDefinitionRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
DEFINITION_ROOT = REPO_ROOT / "data/semantic-models/web-analytics"
SQL_ENDPOINT_TIMEOUT_SECONDS = 300

load_dotenv(ENV_PATH, override=True)

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
LAKEHOUSE_ID = os.getenv("FABRIC_WEB_ANALYTICS_LAKEHOUSE_ID", "").strip()
SEMANTIC_MODEL_NAME = os.getenv(
    "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_NAME", "ContosoWebAnalytics"
)
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com"
).rstrip("/")


def require(value: str, name: str) -> str:
    """Return a required setting or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to create the semantic model.")
    return value


def wait_for_sql_endpoint(
    client: FabricClient,
    workspace_id: str,
    lakehouse_id: str,
):
    """Wait until the lakehouse SQL analytics endpoint is ready."""
    deadline = time.monotonic() + SQL_ENDPOINT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        lakehouse = client.lakehouse.items.get_lakehouse(
            workspace_id, lakehouse_id
        )
        endpoint = lakehouse.properties.sql_endpoint_properties
        if endpoint.provisioning_status == "Success":
            if not endpoint.id or not endpoint.connection_string:
                raise RuntimeError("The SQL endpoint is ready but missing its identity.")
            return endpoint
        if endpoint.provisioning_status == "Failed":
            raise RuntimeError("The web analytics SQL endpoint failed to provision.")
        time.sleep(5)
    raise TimeoutError("Timed out waiting for the web analytics SQL endpoint.")


def definition_part(path: Path, replacements: dict[str, str]):
    """Encode one source-controlled semantic model definition part."""
    relative_path = path.relative_to(DEFINITION_ROOT).as_posix()
    content = path.read_text()
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    if "{{" in content or "}}" in content:
        raise ValueError(f"Unresolved definition marker in {relative_path}.")
    return SemanticModelDefinitionPart(
        path=relative_path,
        payload=base64.b64encode(content.encode()).decode(),
        payload_type="InlineBase64",
    )


def build_definition(sql_endpoint: str, sql_database: str) -> SemanticModelDefinition:
    """Build a TMDL public definition bound to the lakehouse SQL endpoint."""
    source_files = sorted(
        path for path in DEFINITION_ROOT.rglob("*") if path.is_file()
    )
    if not source_files or not (DEFINITION_ROOT / "definition.pbism").exists():
        raise RuntimeError("The web analytics TMDL definition is incomplete.")
    replacements = {
        "{{SQL_ENDPOINT}}": sql_endpoint,
        "{{SQL_DATABASE}}": sql_database,
    }
    return SemanticModelDefinition(
        format="TMDL",
        parts=[definition_part(path, replacements) for path in source_files],
    )


def find_semantic_model(client: FabricClient, workspace_id: str):
    """Find the configured semantic model by display name."""
    return next(
        (
            item
            for item in client.semanticmodel.items.list_semantic_models(workspace_id)
            if item.display_name == SEMANTIC_MODEL_NAME
        ),
        None,
    )


def main() -> None:
    """Create or update the web analytics model and persist its identifiers."""
    tenant_id = require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    lakehouse_id = require(LAKEHOUSE_ID, "FABRIC_WEB_ANALYTICS_LAKEHOUSE_ID")
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        client = FabricClient(credential)
        endpoint = wait_for_sql_endpoint(client, workspace_id, lakehouse_id)
        definition = build_definition(endpoint.connection_string, endpoint.id)
        semantic_model = find_semantic_model(client, workspace_id)
        if semantic_model is None:
            print(f"Creating semantic model '{SEMANTIC_MODEL_NAME}'...")
            semantic_model = client.semanticmodel.items.create_semantic_model(
                workspace_id,
                CreateSemanticModelRequest(
                    display_name=SEMANTIC_MODEL_NAME,
                    description="Direct Lake model for deterministic website analytics.",
                    definition=definition,
                ),
            )
        else:
            print(f"Updating semantic model '{SEMANTIC_MODEL_NAME}'...")
            client.semanticmodel.items.begin_update_semantic_model_definition(
                workspace_id,
                semantic_model.id,
                UpdateSemanticModelDefinitionRequest(definition=definition),
            ).result()
    finally:
        credential.close()

    model_url = (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/datasets/"
        f"{semantic_model.id}/details"
    )
    ENV_PATH.touch()
    for key, value in {
        "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_ID": semantic_model.id,
        "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_NAME": SEMANTIC_MODEL_NAME,
        "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_UI_URL": model_url,
    }.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Web analytics semantic model: {SEMANTIC_MODEL_NAME} ({semantic_model.id})")
    print(f"Power BI UI: {model_url}")


if __name__ == "__main__":
    main()