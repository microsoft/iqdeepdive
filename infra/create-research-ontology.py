"""Create or update the Fabric IQ ontology over the research literature lakehouse.

This is a second retrieval path over the same 20 Delta tables. ``ResearchCitationGraph``
already answers traversals in GQL. An Ontology adds a typed semantic layer with its own
MCP endpoint, so an agent can call ``search_ontology`` against research entities the same
way ``notebooks/fabriciq-ontology-mcp.ipynb`` does against the Contoso DIY data.

The entity and relationship shape is not restated here. It is imported from
``infra/create-research-graph.py``, which already declares every node and edge, its
backing table, its key column, and its properties. That module is the single source of
truth for the schema; this one only translates that declaration into the Ontology
definition format. Adding a concept to the graph therefore adds it to the ontology.

Environment variables (from .env):
  FABRIC_TENANT_ID                  - Microsoft Entra tenant ID for Fabric auth
  FABRIC_WORKSPACE_ID               - Fabric workspace GUID
  RESEARCH_LAKEHOUSE_NAME           - Lakehouse holding the tables
                                      (default: ResearchLiteratureLakehouse)
  FABRIC_RESEARCH_ONTOLOGY_NAME     - Ontology display name
                                      (default: ResearchLiteratureOntology)
  FABRIC_RESEARCH_ONTOLOGY_ID       - Existing ontology GUID to update, if known
  FABRIC_PORTAL_BASE_URL            - Fabric UI host

Written back to .env:
  FABRIC_RESEARCH_ONTOLOGY_ID, FABRIC_RESEARCH_ONTOLOGY_UI_URL,
  FABRIC_RESEARCH_ONTOLOGY_MCP_URL
"""

import base64
import importlib.util
import json
import os
import sys
import time
import uuid
import warnings
from pathlib import Path

from azure.core.exceptions import HttpResponseError
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key

warnings.filterwarnings(
    "ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*"
)

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.ontology.models import (  # noqa: E402
    CreateOntologyRequest,
    OntologyDefinition,
    OntologyDefinitionPart,
    UpdateOntologyDefinitionRequest,
    UpdateOntologyRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"

load_dotenv(ENV_PATH, override=True)

FABRIC_API_URL = "https://api.fabric.microsoft.com"

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
LAKEHOUSE_NAME = os.getenv("RESEARCH_LAKEHOUSE_NAME", "ResearchLiteratureLakehouse")
ONTOLOGY_NAME = os.getenv(
    "FABRIC_RESEARCH_ONTOLOGY_NAME", "ResearchLiteratureOntology"
)
ONTOLOGY_ID = os.getenv("FABRIC_RESEARCH_ONTOLOGY_ID", "").strip()
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://app.fabric.microsoft.com"
).rstrip("/")

# Ontology value types are not the same vocabulary as Graph Model property types.
VALUE_TYPES = {
    "STRING": "String",
    "INT": "BigInt",
    "LONG": "BigInt",
    "DOUBLE": "Double",
    "FLOAT": "Double",
    "BOOLEAN": "Boolean",
}

# The property carrying a human-readable label, in order of preference. Falls back to
# the entity key when a node has none of these.
DISPLAY_PROPERTY_PREFERENCE = ("title", "name", "statement")

ENTITY_ID_BASE = 1001
RELATIONSHIP_ID_BASE = 2001

_CREDENTIAL = None
_FABRIC_CLIENT = None


def require(value: str, name: str) -> str:
    """Return a required value or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to build the research ontology.")
    return value


def get_credential() -> AzureDeveloperCliCredential:
    """Return the shared Fabric credential."""
    global _CREDENTIAL
    if _CREDENTIAL is None:
        _CREDENTIAL = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    return _CREDENTIAL


def get_fabric_client() -> FabricClient:
    """Return the shared Fabric SDK client."""
    global _FABRIC_CLIENT
    if _FABRIC_CLIENT is None:
        _FABRIC_CLIENT = FabricClient(get_credential())
    return _FABRIC_CLIENT


def load_graph_specs() -> tuple[list[dict], list[dict]]:
    """Import the node and edge declarations from the graph builder.

    The graph module's filename is not a valid Python identifier, so it is loaded by
    path. Importing it rather than copying the specs is the point: the ontology and the
    Graph Model then describe the same entities by construction.
    """
    module_path = Path(__file__).with_name("create-research-graph.py")
    spec = importlib.util.spec_from_file_location("research_graph_specs", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load graph specs from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.NODE_SPECS, module.EDGE_SPECS


def definition_part(path: str, payload: dict) -> OntologyDefinitionPart:
    """Wrap a JSON payload as an inline base64 item definition part."""
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return OntologyDefinitionPart(
        path=path, payload=encoded, payload_type="InlineBase64"
    )


def plan_entities(node_specs: list[dict]) -> dict[str, dict]:
    """Assign ontology identifiers to every node spec.

    Returns a map keyed by the graph alias so relationships can resolve both ends. The
    key property is placed first, which makes its identifier predictable and lets a
    relationship bind to it without searching.
    """
    plan = {}
    for offset, node in enumerate(node_specs):
        type_id = ENTITY_ID_BASE + offset
        key_name, key_column = node["key"]
        columns = [(key_name, key_column, "STRING"), *node["properties"]]

        properties = []
        for index, (name, source, graph_type) in enumerate(columns, start=1):
            properties.append(
                {
                    "id": str((type_id * 100) + index),
                    "name": name,
                    "source": source,
                    "valueType": VALUE_TYPES.get(graph_type.upper(), "String"),
                }
            )

        by_name = {prop["name"]: prop["id"] for prop in properties}
        display = next(
            (name for name in DISPLAY_PROPERTY_PREFERENCE if name in by_name),
            key_name,
        )
        plan[node["alias"]] = {
            "id": type_id,
            "name": node["label"],
            "table": node["table"],
            "properties": properties,
            "keyPropertyId": by_name[key_name],
            "displayPropertyId": by_name[display],
        }
    return plan


def entity_parts(
    entity: dict, workspace_id: str, lakehouse_id: str
) -> list[OntologyDefinitionPart]:
    """Build the entity type and its lakehouse table binding."""
    definition = {
        "id": str(entity["id"]),
        "namespace": "usertypes",
        "baseEntityTypeId": None,
        "name": entity["name"],
        "entityIdParts": [entity["keyPropertyId"]],
        "displayNamePropertyId": entity["displayPropertyId"],
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": [
            {"id": prop["id"], "name": prop["name"], "valueType": prop["valueType"]}
            for prop in entity["properties"]
        ],
        "timeseriesProperties": [],
    }
    binding_id = str(uuid.uuid4())
    binding = {
        "id": binding_id,
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "timestampColumnName": None,
            "propertyBindings": [
                {"sourceColumnName": prop["source"], "targetPropertyId": prop["id"]}
                for prop in entity["properties"]
            ],
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": entity["table"],
            },
        },
    }
    return [
        definition_part(f"EntityTypes/{entity['id']}/definition.json", definition),
        definition_part(
            f"EntityTypes/{entity['id']}/DataBindings/{binding_id}.json", binding
        ),
    ]


def relationship_name(label: str) -> str:
    """Convert a graph edge label such as EVALUATES_ON into evaluatesOn."""
    head, *tail = label.lower().split("_")
    return head + "".join(word.capitalize() for word in tail)


def relationship_parts(
    edge: dict,
    relationship_id: int,
    entities: dict[str, dict],
    workspace_id: str,
    lakehouse_id: str,
) -> list[OntologyDefinitionPart]:
    """Build a relationship type and the edge table that contextualizes it."""
    source_alias, source_column = edge["source"]
    target_alias, target_column = edge["destination"]
    source = entities[source_alias]
    target = entities[target_alias]

    definition = {
        "namespace": "usertypes",
        "id": str(relationship_id),
        "name": relationship_name(edge["label"]),
        "namespaceType": "Custom",
        "source": {"entityTypeId": str(source["id"])},
        "target": {"entityTypeId": str(target["id"])},
    }
    contextualization_id = str(uuid.uuid4())
    contextualization = {
        "id": contextualization_id,
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_id,
            "sourceTableName": edge["table"],
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [
            {
                "sourceColumnName": source_column,
                "targetPropertyId": source["keyPropertyId"],
            }
        ],
        "targetKeyRefBindings": [
            {
                "sourceColumnName": target_column,
                "targetPropertyId": target["keyPropertyId"],
            }
        ],
    }
    return [
        definition_part(
            f"RelationshipTypes/{relationship_id}/definition.json", definition
        ),
        definition_part(
            f"RelationshipTypes/{relationship_id}/Contextualizations/"
            f"{contextualization_id}.json",
            contextualization,
        ),
    ]


def build_definition(
    node_specs: list[dict],
    edge_specs: list[dict],
    workspace_id: str,
    lakehouse_id: str,
) -> tuple[OntologyDefinition, dict[str, dict]]:
    """Assemble every definition part for the research ontology."""
    entities = plan_entities(node_specs)
    parts = [
        definition_part(
            ".platform",
            {"metadata": {"type": "Ontology", "displayName": ONTOLOGY_NAME}},
        ),
        definition_part("definition.json", {}),
    ]
    for entity in entities.values():
        parts.extend(entity_parts(entity, workspace_id, lakehouse_id))
    for offset, edge in enumerate(edge_specs):
        parts.extend(
            relationship_parts(
                edge,
                RELATIONSHIP_ID_BASE + offset,
                entities,
                workspace_id,
                lakehouse_id,
            )
        )
    return OntologyDefinition(parts=parts), entities


def find_item_by_name(items, display_name: str):
    """Return the item with the requested display name, if present."""
    return next((item for item in items if item.display_name == display_name), None)


def resolve_lakehouse(workspace_id: str, name: str):
    """Return the lakehouse holding the research tables."""
    lakehouse = find_item_by_name(
        get_fabric_client().lakehouse.items.list_lakehouses(workspace_id), name
    )
    if lakehouse is None:
        raise RuntimeError(
            f"Lakehouse '{name}' was not found in workspace {workspace_id}. "
            "Run infra/create-research-graph.py first."
        )
    return lakehouse


def get_existing_ontology(workspace_id: str, name: str) -> dict | None:
    """Find an ontology in the workspace by display name."""
    for ontology in get_fabric_client().ontology.items.list_ontologies(workspace_id):
        if ontology.display_name == name:
            return {"id": ontology.id, "displayName": ontology.display_name}
    return None


def wait_for_ontology(
    workspace_id: str, name: str, timeout: int = 300, interval: int = 5
) -> dict:
    """Poll for an ontology by display name until its creation operation completes."""
    deadline = time.monotonic() + timeout
    while True:
        existing = get_existing_ontology(workspace_id, name)
        if existing:
            return existing
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out after {timeout}s waiting for ontology '{name}' "
                f"to appear in workspace {workspace_id}."
            )
        time.sleep(interval)


def create_or_get_ontology(workspace_id: str, name: str) -> dict:
    """Create the research ontology item, or reuse the existing one."""
    if ONTOLOGY_ID:
        ontology = get_fabric_client().ontology.items.get_ontology(
            workspace_id, ONTOLOGY_ID
        )
        if ontology.display_name != name:
            print(f"Renaming ontology '{ontology.display_name}' to '{name}'...")
            ontology = get_fabric_client().ontology.items.update_ontology(
                workspace_id, ONTOLOGY_ID, UpdateOntologyRequest(display_name=name)
            )
        print(f"Using existing ontology from FABRIC_RESEARCH_ONTOLOGY_ID: {ontology.id}")
        return {"id": ontology.id, "displayName": ontology.display_name}

    existing = get_existing_ontology(workspace_id, name)
    if existing:
        print(f"Using existing ontology '{name}' ({existing['id']})")
        return existing

    print(f"Creating ontology '{name}'...")
    get_fabric_client().ontology.items.begin_create_ontology(
        workspace_id,
        CreateOntologyRequest(
            display_name=name,
            description=(
                "Typed semantic layer over the research literature lakehouse: papers, "
                "authors, venues, methods, tasks, datasets, metrics, and the "
                "limitations they report."
            ),
        ),
    )
    # The SDK resolves this long running operation through an asynchronous done
    # callback, so the returned result is still empty here. Poll for the item.
    created = wait_for_ontology(workspace_id, name)
    print(f"Created ontology '{name}' ({created['id']})")
    return created


def update_definition(
    workspace_id: str, ontology_id: str, definition: OntologyDefinition
) -> None:
    """Replace the ontology definition with the research entity bindings."""
    print("Updating ontology definition...")
    try:
        get_fabric_client().ontology.items.begin_update_ontology_definition(
            workspace_id,
            ontology_id,
            UpdateOntologyDefinitionRequest(definition=definition),
            update_metadata=False,
        ).result()
    except HttpResponseError as error:
        raise RuntimeError(f"Failed to update ontology definition: {error}") from error
    print("Definition updated.")


def ontology_ui_url(workspace_id: str, ontology_id: str) -> str:
    """Build a direct link to the ontology in the Fabric portal."""
    return (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/ontologies/{ontology_id}"
        "?experience=fabric-developer"
    )


def ontology_mcp_url(workspace_id: str, ontology_id: str) -> str:
    """Build the Fabric Ontology MCP server endpoint."""
    return (
        f"{FABRIC_API_URL}/v1/mcp/dataPlane/"
        f"workspaces/{workspace_id}/items/{ontology_id}/ontologyEndpoint"
    )


def write_env(values: dict[str, str]) -> None:
    """Persist generated identifiers to the repo root .env."""
    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="never")


def main() -> None:
    """Create or update the research ontology and record how to reach it."""
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")

    node_specs, edge_specs = load_graph_specs()
    # The graph module loads .env from its own path; keep this script's view current.
    load_dotenv(ENV_PATH, override=True)

    print(f"Workspace:  {workspace_id}")
    print(f"Lakehouse:  {LAKEHOUSE_NAME}")
    print(f"Ontology:   {ONTOLOGY_NAME}")

    lakehouse = resolve_lakehouse(workspace_id, LAKEHOUSE_NAME)
    print(f"Lakehouse resolved: {lakehouse.id}")

    definition, entities = build_definition(
        node_specs, edge_specs, workspace_id, lakehouse.id
    )
    print(
        f"Built {len(entities)} entity types and {len(edge_specs)} relationship types "
        f"across {len(definition.parts)} definition parts."
    )
    for entity in entities.values():
        print(f"  {entity['name']:<12} <- {entity['table']}")

    ontology = create_or_get_ontology(workspace_id, ONTOLOGY_NAME)
    update_definition(workspace_id, ontology["id"], definition)

    ui_url = ontology_ui_url(workspace_id, ontology["id"])
    mcp_url = ontology_mcp_url(workspace_id, ontology["id"])
    write_env(
        {
            "FABRIC_RESEARCH_ONTOLOGY_ID": ontology["id"],
            "FABRIC_RESEARCH_ONTOLOGY_UI_URL": ui_url,
            "FABRIC_RESEARCH_ONTOLOGY_MCP_URL": mcp_url,
        }
    )

    print()
    print(f"Ontology ID: {ontology['id']}")
    print(f"Fabric UI:   {ui_url}")
    print(f"MCP:         {mcp_url}")


if __name__ == "__main__":
    main()
