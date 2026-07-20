"""
Create a Fabric Lakehouse and load the Contoso DIY dataset as Delta tables.

This script:
1. Creates a lakehouse in Microsoft Fabric using the Microsoft Fabric API SDK
2. Downloads the Contoso DIY sample data (product_data.json, reference_data.json) from GitHub
3. Flattens and enriches the data with inventory and supplier relationships
4. Uploads CSVs to OneLake (lakehouse Files section)
5. Loads CSVs as Delta tables using the Load Table API
6. Creates or updates a Fabric IQ ontology bound to the lakehouse tables

Environment variables (from .env):
  FABRIC_WORKSPACE_ID  - Existing Fabric workspace GUID
  FABRIC_CAPACITY_ID   - Fabric capacity GUID or ARM resource ID for workspace creation
    FABRIC_TENANT_ID     - Required Microsoft Entra tenant ID for Fabric auth
    FABRIC_PORTAL_BASE_URL - Fabric UI host (default: https://msit.powerbi.com)
    LAKEHOUSE_NAME       - Name for the lakehouse (default: ContosoDIYLakehouse)
  FABRIC_ONTOLOGY_ID   - Existing ontology GUID to update, if known
    FABRIC_ONTOLOGY_NAME - Name for the ontology (default: ContosoDIYOntology)
        FABRIC_ONTOLOGY_UI_URL - Generated direct link to the ontology in Fabric
    FABRIC_ONTOLOGY_MCP_URL - Generated ontology MCP server endpoint
  CREATE_ONTOLOGY      - Create/update ontology after table load (default: true)
  INCLUDE_EMBEDDINGS   - Include vector embeddings in products table (default: false)
"""

import base64
import csv
import hashlib
import io
import json
import os
import sys
import traceback
import uuid
import warnings
from datetime import datetime, timedelta

import requests
from azure.core.exceptions import HttpResponseError
from azure.identity import AzureDeveloperCliCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv, set_key

warnings.filterwarnings(
    "ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*"
)

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.core.models import (  # noqa: E402
    AddWorkspaceRoleAssignmentRequest,
    CreateWorkspaceRequest,
    Principal,
)
from microsoft_fabric_api.generated.lakehouse.models import (  # noqa: E402
    CreateLakehouseRequest,
    Csv,
    LoadTableRequest,
)
from microsoft_fabric_api.generated.ontology.models import (  # noqa: E402
    CreateOntologyRequest,
    OntologyDefinition,
    OntologyDefinitionPart,
    UpdateOntologyDefinitionRequest,
    UpdateOntologyRequest,
)

load_dotenv(override=True)

# Configuration
ONELAKE_DFS_URL = "https://onelake.dfs.fabric.microsoft.com"

GITHUB_CONTENTS_BASE = "https://api.github.com/repositories/1021950905/contents/data/database"

WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
LAKEHOUSE_NAME = os.getenv("LAKEHOUSE_NAME", "ContosoDIYLakehouse")
WORKSPACE_NAME = os.getenv("FABRIC_WORKSPACE_NAME", "ContosoDIYWorkspace")
FABRIC_CAPACITY_ID = os.getenv("FABRIC_CAPACITY_ID", "")
FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com"
).rstrip("/")
FABRIC_ONTOLOGY_ID = os.getenv("FABRIC_ONTOLOGY_ID", "")
FABRIC_ONTOLOGY_NAME = os.getenv("FABRIC_ONTOLOGY_NAME", "ContosoDIYOntology")
FABRIC_LAB_USER_UPN = os.getenv("FABRIC_LAB_USER_UPN", "")
FABRIC_LAB_USER_OID = os.getenv("FABRIC_LAB_USER_OID", "")
CREATE_ONTOLOGY = os.getenv("CREATE_ONTOLOGY", "true").lower() == "true"
INCLUDE_EMBEDDINGS = os.getenv("INCLUDE_EMBEDDINGS", "false").lower() == "true"
_CREDENTIAL = None
_FABRIC_CLIENT = None

STORE_DETAILS = {
    "Seattle": ("STORE-SEA", "Seattle", "WA", "Puget Sound", "Retail"),
    "Bellevue": ("STORE-BEL", "Bellevue", "WA", "Puget Sound", "Retail"),
    "Tacoma": ("STORE-TAC", "Tacoma", "WA", "Puget Sound", "Retail"),
    "Spokane": ("STORE-SPO", "Spokane", "WA", "Eastern Washington", "Retail"),
    "Everett": ("STORE-EVE", "Everett", "WA", "Puget Sound", "Retail"),
    "Redmond": ("STORE-RED", "Redmond", "WA", "Puget Sound", "Retail"),
    "Kirkland": ("STORE-KIR", "Kirkland", "WA", "Puget Sound", "Retail"),
    "Online": ("STORE-ONL", "Online", "WA", "Digital", "Online"),
}

SUPPLIER_NAMES = (
    "Cascadia Toolworks",
    "Rainier Building Supply",
    "Puget Hardware Partners",
    "Evergreen Electrical",
    "Olympic Outdoor Goods",
    "Northwest Paint and Finish",
    "Columbia Plumbing Supply",
    "Sound Safety Equipment",
    "Summit Fastener Group",
    "Harbor Home Products",
    "Cascade Garden Supply",
    "Pacific Lighting Works",
    "Redwood Storage Systems",
    "Blue Mountain Lumber",
    "Orca Power Tools",
    "Pioneer Flooring Supply",
    "Atlas Workshop Equipment",
    "Cedar Ridge Millworks",
    "Frontier Climate Solutions",
    "Northstar Appliance Parts",
    "Granite Peak Industrial",
    "Salish Home Improvement",
    "Copper River Fixtures",
    "Metro DIY Distribution",
)

# Logging
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "create-lakehouse.log")
REPO_ROOT = os.path.dirname(SCRIPT_DIR)


def update_root_env(values: dict):
    """Update the repo root .env file with key=value pairs (append or replace)."""
    env_path = os.path.join(REPO_ROOT, ".env")
    for key, val in values.items():
        set_key(env_path, key, val)


def log_message(message: str):
    """Write message to log file and stdout with timestamp."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_ontology_ui_url(workspace_id: str, ontology_id: str) -> str:
    """Build a direct link to an ontology in the Fabric UI."""
    return (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/ontologies/{ontology_id}"
        "?experience=fabric-developer"
    )


def get_ontology_mcp_url(workspace_id: str, ontology_id: str) -> str:
    """Build the Fabric Ontology MCP server endpoint."""
    return (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/"
        f"workspaces/{workspace_id}/items/{ontology_id}/ontologyEndpoint"
    )


def get_credential():
    """Get the credential used for Fabric API and OneLake calls."""
    global _CREDENTIAL
    if not FABRIC_TENANT_ID:
        raise RuntimeError("FABRIC_TENANT_ID is required for Fabric authentication.")
    if _CREDENTIAL is None:
        _CREDENTIAL = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    return _CREDENTIAL


def get_fabric_client() -> FabricClient:
    """Get the Fabric SDK client using the configured tenant credential."""
    global _FABRIC_CLIENT
    if _FABRIC_CLIENT is None:
        _FABRIC_CLIENT = FabricClient(get_credential())
    return _FABRIC_CLIENT


def is_http_status(error: HttpResponseError, status_code: int) -> bool:
    """Return whether an SDK error has the specified HTTP status."""
    return error.status_code == status_code


def resolve_capacity_id(capacity_id_or_arm: str) -> str:
    """Resolve ARM resource ID or Fabric capacity GUID to the Fabric GUID."""
    # If it's already a GUID (no slashes), return as-is
    if "/" not in capacity_id_or_arm:
        return capacity_id_or_arm

    # It's an ARM resource ID — look up the Fabric GUID via the capacities API
    log_message("Resolving ARM capacity ID to Fabric GUID...")
    # Extract capacity name from ARM ID (last segment)
    arm_name = capacity_id_or_arm.rstrip("/").split("/")[-1]
    for capacity in get_fabric_client().core.capacities.list_capacities():
        if capacity.display_name == arm_name:
            log_message(f"Resolved capacity: {capacity.id} ({capacity.display_name})")
            return capacity.id

    log_message(f"ERROR: Could not find Fabric capacity matching '{arm_name}'")
    sys.exit(1)


def create_workspace(name: str, capacity_id: str) -> dict:
    """Create a Fabric workspace assigned to the given capacity."""
    log_message(f"Creating workspace '{name}' on capacity {capacity_id[:12]}...")
    try:
        workspace = get_fabric_client().core.workspaces.create_workspace(
            CreateWorkspaceRequest(display_name=name, capacity_id=capacity_id)
        )
        log_message(f"Workspace created: {workspace.id}")
        return {"id": workspace.id, "displayName": workspace.display_name}
    except HttpResponseError as error:
        if is_http_status(error, 409):
            log_message(f"Workspace '{name}' already exists. Fetching existing...")
            return get_existing_workspace(name)
        raise


def get_existing_workspace(name: str) -> dict:
    """Find an existing workspace by name."""
    for workspace in get_fabric_client().core.workspaces.list_workspaces():
        if workspace.display_name == name:
            log_message(f"Found existing workspace: {workspace.id}")
            return {"id": workspace.id, "displayName": workspace.display_name}
    log_message(f"ERROR: Workspace '{name}' not found.")
    sys.exit(1)


def add_workspace_member(workspace_id: str, user_oid: str, user_email: str, role: str = "Admin"):
    """Add a user to the workspace with the given role (Admin, Member, Contributor, Viewer)."""
    log_message(f"Adding '{user_email}' (OID: {user_oid}) as {role} to workspace {workspace_id[:12]}...")
    try:
        get_fabric_client().core.workspaces.add_workspace_role_assignment(
            workspace_id,
            AddWorkspaceRoleAssignmentRequest(
                principal=Principal(id=user_oid),
                role=role,
            ),
        )
        log_message(f"User added as {role} successfully.")
    except HttpResponseError as error:
        if is_http_status(error, 409):
            log_message("User already has a role assignment on this workspace.")
        else:
            log_message(f"WARNING: Failed to add user to workspace: {error}")


def create_lakehouse(workspace_id: str, name: str) -> dict:
    """Create a lakehouse in the specified workspace."""
    log_message(f"Creating lakehouse '{name}'...")
    try:
        lakehouse = get_fabric_client().lakehouse.items.begin_create_lakehouse(
            workspace_id, CreateLakehouseRequest(display_name=name)
        ).result()
        log_message(f"Lakehouse created: {lakehouse.id}")
        return {"id": lakehouse.id, "displayName": lakehouse.display_name}
    except HttpResponseError as error:
        if is_http_status(error, 409):
            log_message(f"Lakehouse '{name}' already exists. Fetching existing...")
            return get_existing_lakehouse(workspace_id, name)
        if is_http_status(error, 401) and "UserNotLicensed" in str(error):
            log_message(
                "Your signed-in Microsoft Entra account is not licensed for Fabric. "
                "Assign it a Fabric/Power BI license or activate a Fabric trial, "
                "then retry after confirming the account has access to this workspace."
            )
        raise


def get_existing_lakehouse(workspace_id: str, name: str) -> dict:
    """Find an existing lakehouse by name."""
    for lakehouse in get_fabric_client().lakehouse.items.list_lakehouses(workspace_id):
        if lakehouse.display_name == name:
            log_message(f"Found existing lakehouse: {lakehouse.id}")
            return {"id": lakehouse.id, "displayName": lakehouse.display_name}
    log_message(f"ERROR: Lakehouse '{name}' not found in workspace.")
    sys.exit(1)


def get_lakehouse_properties(workspace_id: str, lakehouse_id: str) -> dict:
    """Get lakehouse properties including OneLake paths."""
    lakehouse = get_fabric_client().lakehouse.items.get_lakehouse(workspace_id, lakehouse_id)
    return lakehouse.as_dict()


def download_json(filename: str) -> dict:
    """Download a JSON file from the GitHub repository."""
    url = f"{GITHUB_CONTENTS_BASE}/{filename}"
    log_message(f"Downloading {filename}...")
    resp = requests.get(
        url,
        headers={"Accept": "application/vnd.github.raw+json"},
        params={"ref": "main"},
        timeout=120,
    )
    resp.raise_for_status()
    return normalize_company_locations(resp.json())


def normalize_company_locations(value):
    """Normalize company-prefixed retail locations in nested sample data."""
    if isinstance(value, dict):
        return {key: normalize_company_locations(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_company_locations(item) for item in value]
    if isinstance(value, str) and " Retail " in value:
        _, separator, location = value.partition(" Retail ")
        return f"Contoso{separator}{location}"
    return value


def flatten_products(product_data: dict) -> list[dict]:
    """Flatten nested product_data.json into a flat list of product records."""
    rows = []
    categories = product_data.get("main_categories", {})
    for category_name, category_data in categories.items():
        seasonal = category_data.get("washington_seasonal_multipliers", [])
        seasonal_str = ";".join(str(s) for s in seasonal) if seasonal else ""

        for product_type_name, products in category_data.items():
            if product_type_name == "washington_seasonal_multipliers":
                continue
            if not isinstance(products, list):
                continue
            for product in products:
                row = {
                    "category": category_name,
                    "product_type": product_type_name,
                    "name": product.get("name", ""),
                    "sku": product.get("sku", ""),
                    "price": product.get("price", 0),
                    "description": product.get("description", ""),
                    "image_path": product.get("image_path", ""),
                    "seasonal_multipliers": seasonal_str,
                }
                if INCLUDE_EMBEDDINGS:
                    img_emb = product.get("image_embedding", [])
                    desc_emb = product.get("description_embedding", [])
                    row["image_embedding"] = json.dumps(img_emb) if img_emb else ""
                    row["description_embedding"] = (
                        json.dumps(desc_emb) if desc_emb else ""
                    )
                rows.append(row)
    return rows


def flatten_stores(reference_data: dict) -> list[dict]:
    """Flatten stores from reference_data.json."""
    rows = []
    for store_name, config in reference_data.get("stores", {}).items():
        location_name = store_name.rsplit(" ", 1)[-1]
        store_id, city, state, region, store_type = STORE_DETAILS[location_name]
        rows.append(
            {
                "store_id": store_id,
                "store_name": store_name,
                "city": city,
                "state": state,
                "region": region,
                "store_type": store_type,
                "rls_user_id": config.get("rls_user_id", ""),
                "customer_distribution_weight": config.get(
                    "customer_distribution_weight", 0
                ),
                "order_frequency_multiplier": config.get(
                    "order_frequency_multiplier", 0
                ),
                "order_value_multiplier": config.get("order_value_multiplier", 0),
            }
        )
    return rows


def stable_int(*values: object, modulo: int) -> int:
    """Return a deterministic integer derived from the supplied values."""
    text = "|".join(str(value) for value in values)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def generate_inventory(products: list[dict], stores: list[dict]) -> list[dict]:
    """Generate deterministic store-level product inventory."""
    rows = []
    reference_date = datetime(2026, 6, 30)

    for store in stores:
        is_online = store["store_type"] == "Online"
        assortment_percent = min(
            80, 50 + int(store["customer_distribution_weight"])
        )
        for product in products:
            sku = product["sku"]
            if not is_online and stable_int(
                store["store_id"], sku, "assortment", modulo=100
            ) >= assortment_percent:
                continue

            quantity_on_hand = stable_int(
                store["store_id"], sku, "on-hand", modulo=121
            )
            if is_online:
                quantity_on_hand += 30
            quantity_reserved = stable_int(
                store["store_id"], sku, "reserved", modulo=13
            )
            quantity_reserved = min(quantity_reserved, quantity_on_hand)
            reorder_point = 10 + stable_int(
                store["store_id"], sku, "reorder-point", modulo=21
            )
            last_restocked = reference_date - timedelta(
                days=stable_int(store["store_id"], sku, "restocked", modulo=91)
            )
            rows.append(
                {
                    "inventory_id": f"INV-{store['store_id']}-{sku}",
                    "store_id": store["store_id"],
                    "sku": sku,
                    "quantity_on_hand": quantity_on_hand,
                    "quantity_reserved": quantity_reserved,
                    "available_quantity": quantity_on_hand - quantity_reserved,
                    "reorder_point": reorder_point,
                    "reorder_quantity": 24
                    + stable_int(
                        store["store_id"], sku, "reorder-quantity", modulo=73
                    ),
                    "last_restocked_at": last_restocked.strftime(
                        "%Y-%m-%dT00:00:00Z"
                    ),
                }
            )
    return rows


def generate_suppliers() -> list[dict]:
    """Generate deterministic supplier reference data."""
    rows = []
    for index, supplier_name in enumerate(SUPPLIER_NAMES, start=1):
        supplier_id = f"SUP-{index:03d}"
        rows.append(
            {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "contact_email": f"orders@{supplier_name.lower().replace(' ', '')}.example",
                "lead_time_days": 2
                + stable_int(supplier_id, "lead-time", modulo=13),
                "reliability_rating": round(
                    3.5 + stable_int(supplier_id, "rating", modulo=151) / 100,
                    2,
                ),
            }
        )
    return rows


def generate_product_suppliers(
    products: list[dict], suppliers: list[dict]
) -> list[dict]:
    """Assign every product a primary and optional secondary supplier."""
    rows = []
    for product_index, product in enumerate(
        sorted(products, key=lambda row: row["sku"])
    ):
        primary_index = product_index % len(suppliers)
        supplier_indexes = [primary_index]
        if stable_int(product["sku"], "secondary-supplier", modulo=100) < 45:
            offset = 1 + stable_int(
                product["sku"], "supplier-offset", modulo=len(suppliers) - 1
            )
            supplier_indexes.append((primary_index + offset) % len(suppliers))

        for position, supplier_index in enumerate(supplier_indexes):
            supplier = suppliers[supplier_index]
            cost_ratio = 0.45 + stable_int(
                product["sku"], supplier["supplier_id"], "cost", modulo=28
            ) / 100
            rows.append(
                {
                    "supplier_id": supplier["supplier_id"],
                    "sku": product["sku"],
                    "supplier_sku": f"{supplier['supplier_id']}-{product['sku']}",
                    "unit_cost": round(float(product["price"]) * cost_ratio, 2),
                    "is_primary": position == 0,
                }
            )
    return rows


def validate_retail_graph(
    products: list[dict],
    stores: list[dict],
    inventory: list[dict],
    suppliers: list[dict],
    product_suppliers: list[dict],
) -> None:
    """Validate keys and relationship endpoints before uploading generated data."""
    product_ids = {row["sku"] for row in products}
    store_ids = {row["store_id"] for row in stores}
    supplier_ids = {row["supplier_id"] for row in suppliers}

    if len(product_ids) != len(products):
        raise ValueError("Product SKUs must be unique.")
    if len(store_ids) != len(stores):
        raise ValueError("Store IDs must be unique.")
    if len(supplier_ids) != len(suppliers):
        raise ValueError("Supplier IDs must be unique.")
    if not all(
        row["store_id"] in store_ids and row["sku"] in product_ids
        for row in inventory
    ):
        raise ValueError("Inventory contains an unknown store or product key.")
    if {row["sku"] for row in inventory} != product_ids:
        raise ValueError("Every product must have at least one inventory record.")
    if not all(
        row["supplier_id"] in supplier_ids and row["sku"] in product_ids
        for row in product_suppliers
    ):
        raise ValueError("Product suppliers contain an unknown endpoint key.")
    if {row["sku"] for row in product_suppliers} != product_ids:
        raise ValueError("Every product must have at least one supplier.")


def flatten_year_weights(reference_data: dict) -> list[dict]:
    """Flatten year weights from reference_data.json."""
    rows = []
    for year, weight in reference_data.get("year_weights", {}).items():
        rows.append({"year": int(year), "weight": weight})
    return rows


def flatten_categories(product_data: dict) -> list[dict]:
    """Extract unique categories with their seasonal multipliers."""
    rows = []
    categories = product_data.get("main_categories", {})
    for category_name, category_data in categories.items():
        seasonal = category_data.get("washington_seasonal_multipliers", [])
        row = {"category_name": category_name}
        for i, month in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun",
             "jul", "aug", "sep", "oct", "nov", "dec"]
        ):
            row[f"multiplier_{month}"] = seasonal[i] if i < len(seasonal) else 1.0
        rows.append(row)
    return rows


def to_csv_bytes(rows: list[dict]) -> bytes:
    """Convert a list of dicts to CSV bytes."""
    if not rows:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def upload_to_onelake(
    workspace_id: str, lakehouse_id: str, filename: str, data: bytes
):
    """Upload a file to the lakehouse Files section via OneLake ADLS SDK."""
    service_client = DataLakeServiceClient(
        account_url=ONELAKE_DFS_URL, credential=get_credential()
    )

    filesystem_name = workspace_id
    directory_path = f"{lakehouse_id}/Files"

    file_system_client = service_client.get_file_system_client(filesystem_name)
    directory_client = file_system_client.get_directory_client(directory_path)
    file_client = directory_client.get_file_client(filename)

    log_message(f"Uploading {filename} ({len(data):,} bytes)...")
    file_client.upload_data(data, overwrite=True)
    log_message(f"Uploaded {filename}")


def load_table(
    workspace_id: str, lakehouse_id: str, table_name: str, filename: str
) -> bool:
    """Load a CSV file into a Delta table and wait for completion."""
    log_message(f"Loading table '{table_name}' from {filename}...")
    try:
        get_fabric_client().lakehouse.tables.begin_load_table(
            workspace_id,
            lakehouse_id,
            table_name,
            LoadTableRequest(
                relative_path=f"Files/{filename}",
                path_type="File",
                mode="Overwrite",
                format_options=Csv(header=True, delimiter=","),
            ),
        ).result()
        log_message(f"Load completed for '{table_name}'")
        return True
    except HttpResponseError as error:
        log_message(f"ERROR: Load failed for '{table_name}': {error}")
        return False


def create_definition_part(path: str, payload: dict) -> OntologyDefinitionPart:
    """Create a Fabric item definition part with an inline base64 JSON payload."""
    json_payload = json.dumps(payload, separators=(",", ":"))
    encoded = base64.b64encode(json_payload.encode("utf-8")).decode("ascii")
    return OntologyDefinitionPart(
        path=path, payload=encoded, payload_type="InlineBase64"
    )


def make_entity_parts(
    entity_id: int,
    entity_name: str,
    table_name: str,
    columns: list[dict],
    key_property: str,
    display_property: str,
    workspace_id: str,
    lakehouse_id: str,
) -> list[OntologyDefinitionPart]:
    """Build ontology entity and lakehouse table binding definition parts."""
    properties = []
    property_bindings = []
    for offset, column in enumerate(columns, start=1):
        property_id = str(column.get("id", (entity_id * 100) + offset))
        properties.append(
            {
                "id": property_id,
                "name": column["name"],
                "valueType": column["type"],
            }
        )
        property_bindings.append(
            {
                "sourceColumnName": column["source"],
                "targetPropertyId": property_id,
            }
        )

    property_ids = {prop["name"]: prop["id"] for prop in properties}
    definition = {
        "id": str(entity_id),
        "namespace": "usertypes",
        "baseEntityTypeId": None,
        "name": entity_name,
        "entityIdParts": [property_ids[key_property]],
        "displayNamePropertyId": property_ids[display_property],
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": properties,
        "timeseriesProperties": [],
    }
    binding_id = str(uuid.uuid4())
    binding = {
        "id": binding_id,
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "timestampColumnName": None,
            "propertyBindings": property_bindings,
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": table_name,
            },
        },
    }
    return [
        create_definition_part(f"EntityTypes/{entity_id}/definition.json", definition),
        create_definition_part(
            f"EntityTypes/{entity_id}/DataBindings/{binding_id}.json", binding
        ),
    ]


def make_relationship_parts(
    relationship_id: int,
    relationship_name: str,
    source_entity_id: int,
    source_column: str,
    source_property_id: str,
    target_entity_id: int,
    target_column: str,
    target_property_id: str,
    table_name: str,
    workspace_id: str,
    lakehouse_id: str,
) -> list[OntologyDefinitionPart]:
    """Build an ontology relationship type and its lakehouse contextualization."""
    definition = {
        "namespace": "usertypes",
        "id": str(relationship_id),
        "name": relationship_name,
        "namespaceType": "Custom",
        "source": {"entityTypeId": str(source_entity_id)},
        "target": {"entityTypeId": str(target_entity_id)},
    }
    contextualization_id = str(uuid.uuid4())
    contextualization = {
        "id": contextualization_id,
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_id,
            "sourceTableName": table_name,
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [
            {
                "sourceColumnName": source_column,
                "targetPropertyId": source_property_id,
            }
        ],
        "targetKeyRefBindings": [
            {
                "sourceColumnName": target_column,
                "targetPropertyId": target_property_id,
            }
        ],
    }
    return [
        create_definition_part(
            f"RelationshipTypes/{relationship_id}/definition.json", definition
        ),
        create_definition_part(
            f"RelationshipTypes/{relationship_id}/Contextualizations/"
            f"{contextualization_id}.json",
            contextualization,
        ),
    ]


def build_contoso_ontology_definition(
    workspace_id: str, lakehouse_id: str
) -> OntologyDefinition:
    """Build a Fabric IQ ontology definition for the Contoso DIY lakehouse tables."""
    parts = [
        create_definition_part(
            ".platform",
            {"metadata": {"type": "Ontology", "displayName": FABRIC_ONTOLOGY_NAME}},
        ),
        create_definition_part("definition.json", {}),
    ]

    # Ontology property names must be identifier-safe; source maps to lakehouse column names.
    entity_specs = [
        (
            1001,
            "Product",
            "products",
            [
                {"name": "sku", "source": "sku", "type": "String"},
                {"name": "name", "source": "name", "type": "String"},
                {"name": "category", "source": "category", "type": "String"},
                {"name": "productType", "source": "product_type", "type": "String"},
                {"name": "price", "source": "price", "type": "Double"},
                {"name": "description", "source": "description", "type": "String"},
                {
                    "id": 100108,
                    "name": "imagePath",
                    "source": "image_path",
                    "type": "String",
                },
                {
                    "id": 100109,
                    "name": "seasonalMultipliers",
                    "source": "seasonal_multipliers",
                    "type": "String",
                },
            ],
            "sku",
            "name",
        ),
        (
            1002,
            "Category",
            "categories",
            [
                {"name": "categoryName", "source": "category_name", "type": "String"},
                {"name": "multiplierJan", "source": "multiplier_jan", "type": "Double"},
                {"name": "multiplierFeb", "source": "multiplier_feb", "type": "Double"},
                {"name": "multiplierMar", "source": "multiplier_mar", "type": "Double"},
                {"name": "multiplierApr", "source": "multiplier_apr", "type": "Double"},
                {"name": "multiplierMay", "source": "multiplier_may", "type": "Double"},
                {"name": "multiplierJun", "source": "multiplier_jun", "type": "Double"},
                {"name": "multiplierJul", "source": "multiplier_jul", "type": "Double"},
                {"name": "multiplierAug", "source": "multiplier_aug", "type": "Double"},
                {"name": "multiplierSep", "source": "multiplier_sep", "type": "Double"},
                {"name": "multiplierOct", "source": "multiplier_oct", "type": "Double"},
                {"name": "multiplierNov", "source": "multiplier_nov", "type": "Double"},
                {"name": "multiplierDec", "source": "multiplier_dec", "type": "Double"},
            ],
            "categoryName",
            "categoryName",
        ),
        (
            1003,
            "Store",
            "stores",
            [
                {"name": "storeName", "source": "store_name", "type": "String"},
                {"name": "rlsUserId", "source": "rls_user_id", "type": "String"},
                {
                    "name": "customerDistributionWeight",
                    "source": "customer_distribution_weight",
                    "type": "Double",
                },
                {
                    "name": "orderFrequencyMultiplier",
                    "source": "order_frequency_multiplier",
                    "type": "Double",
                },
                {
                    "name": "orderValueMultiplier",
                    "source": "order_value_multiplier",
                    "type": "Double",
                },
                {"name": "storeId", "source": "store_id", "type": "String"},
                {"name": "city", "source": "city", "type": "String"},
                {"name": "state", "source": "state", "type": "String"},
                {"name": "region", "source": "region", "type": "String"},
                {"name": "storeType", "source": "store_type", "type": "String"},
            ],
            "storeId",
            "storeName",
        ),
        (
            1005,
            "Inventory",
            "inventory",
            [
                {
                    "name": "inventoryId",
                    "source": "inventory_id",
                    "type": "String",
                },
                {"name": "storeId", "source": "store_id", "type": "String"},
                {"name": "sku", "source": "sku", "type": "String"},
                {
                    "name": "quantityOnHand",
                    "source": "quantity_on_hand",
                    "type": "BigInt",
                },
                {
                    "name": "quantityReserved",
                    "source": "quantity_reserved",
                    "type": "BigInt",
                },
                {
                    "name": "availableQuantity",
                    "source": "available_quantity",
                    "type": "BigInt",
                },
                {
                    "name": "reorderPoint",
                    "source": "reorder_point",
                    "type": "BigInt",
                },
                {
                    "name": "reorderQuantity",
                    "source": "reorder_quantity",
                    "type": "BigInt",
                },
                {
                    "name": "lastRestockedAt",
                    "source": "last_restocked_at",
                    "type": "DateTime",
                },
            ],
            "inventoryId",
            "inventoryId",
        ),
        (
            1006,
            "Supplier",
            "suppliers",
            [
                {
                    "name": "supplierId",
                    "source": "supplier_id",
                    "type": "String",
                },
                {
                    "name": "supplierName",
                    "source": "supplier_name",
                    "type": "String",
                },
                {
                    "name": "contactEmail",
                    "source": "contact_email",
                    "type": "String",
                },
                {
                    "name": "leadTimeDays",
                    "source": "lead_time_days",
                    "type": "BigInt",
                },
                {
                    "name": "reliabilityRating",
                    "source": "reliability_rating",
                    "type": "Double",
                },
            ],
            "supplierId",
            "supplierName",
        ),
    ]

    for spec in entity_specs:
        parts.extend(make_entity_parts(*spec, workspace_id, lakehouse_id))

    parts.extend(
        make_relationship_parts(
            relationship_id=2001,
            relationship_name="contains",
            source_entity_id=1002,
            source_column="category",
            source_property_id="100201",
            target_entity_id=1001,
            target_column="sku",
            target_property_id="100101",
            table_name="products",
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
        )
    )
    parts.extend(
        make_relationship_parts(
            relationship_id=2002,
            relationship_name="holds",
            source_entity_id=1003,
            source_column="store_id",
            source_property_id="100306",
            target_entity_id=1005,
            target_column="inventory_id",
            target_property_id="100501",
            table_name="inventory",
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
        )
    )
    parts.extend(
        make_relationship_parts(
            relationship_id=2003,
            relationship_name="recordsStockFor",
            source_entity_id=1005,
            source_column="inventory_id",
            source_property_id="100501",
            target_entity_id=1001,
            target_column="sku",
            target_property_id="100101",
            table_name="inventory",
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
        )
    )
    parts.extend(
        make_relationship_parts(
            relationship_id=2004,
            relationship_name="supplies",
            source_entity_id=1006,
            source_column="supplier_id",
            source_property_id="100601",
            target_entity_id=1001,
            target_column="sku",
            target_property_id="100101",
            table_name="product_suppliers",
            workspace_id=workspace_id,
            lakehouse_id=lakehouse_id,
        )
    )

    return OntologyDefinition(parts=parts)


def get_existing_ontology(workspace_id: str, name: str) -> dict | None:
    """Find an existing ontology in the specified workspace by display name."""
    for ontology in get_fabric_client().ontology.items.list_ontologies(workspace_id):
        if ontology.display_name == name:
            return {"id": ontology.id, "displayName": ontology.display_name}
    return None


def create_or_get_ontology(workspace_id: str, name: str) -> dict:
    """Create a Fabric IQ ontology item, or reuse an existing one with the same name."""
    if FABRIC_ONTOLOGY_ID:
        ontology = get_fabric_client().ontology.items.get_ontology(
            workspace_id, FABRIC_ONTOLOGY_ID
        )
        if ontology.display_name != name:
            log_message(
                f"Renaming ontology '{ontology.display_name}' to '{name}'..."
            )
            ontology = get_fabric_client().ontology.items.update_ontology(
                workspace_id,
                FABRIC_ONTOLOGY_ID,
                UpdateOntologyRequest(display_name=name),
            )
        log_message(
            f"Using existing ontology ID from FABRIC_ONTOLOGY_ID: {ontology.id}"
        )
        return {"id": ontology.id, "displayName": ontology.display_name}

    existing = get_existing_ontology(workspace_id, name)
    if existing:
        log_message(f"Found existing ontology: {existing['id']}")
        return existing

    log_message(f"Creating ontology '{name}'...")
    ontology = get_fabric_client().ontology.items.begin_create_ontology(
        workspace_id,
        CreateOntologyRequest(
            display_name=name,
            description="Ontology for the Contoso DIY lakehouse data.",
        ),
    ).result()
    log_message(f"Ontology created: {ontology.id}")
    return {"id": ontology.id, "displayName": ontology.display_name}


def update_ontology_definition(
    workspace_id: str, ontology_id: str, lakehouse_id: str
) -> bool:
    """Replace the ontology definition with Contoso DIY lakehouse entity bindings."""
    log_message("Updating ontology definition with Contoso DIY entity bindings...")
    try:
        get_fabric_client().ontology.items.begin_update_ontology_definition(
            workspace_id,
            ontology_id,
            UpdateOntologyDefinitionRequest(
                definition=build_contoso_ontology_definition(
                    workspace_id, lakehouse_id
                )
            ),
            update_metadata=False,
        ).result()
        return True
    except HttpResponseError as error:
        log_message(
            f"ERROR: Failed to update ontology definition: {error}"
        )
        return False


def main():
    """Main execution flow."""
    log_message("=" * 60)
    log_message("Fabric Lakehouse Creator - Contoso DIY Dataset")
    log_message("=" * 60)

    # Resolve workspace: use provided ID, or create one on the given capacity
    workspace_id = WORKSPACE_ID
    if not workspace_id and FABRIC_CAPACITY_ID:
        # Only resolve the capacity when we actually need it (to create a workspace)
        capacity_guid = resolve_capacity_id(FABRIC_CAPACITY_ID)
        # Persist the resolved GUID immediately so it's available even if later step fails
        update_root_env({"FABRIC_CAPACITY_ID": capacity_guid})
        log_message("Updated repo root .env with FABRIC_CAPACITY_ID")
        log_message("No workspace ID provided. Creating workspace on Fabric capacity...")
        # Use a unique workspace name to avoid collisions with leftover
        # workspaces from previous lab sessions in the same tenant.
        workspace_name = f"ContosoDIY-{uuid.uuid4().hex[:8]}"
        ws = create_workspace(workspace_name, capacity_guid)
        workspace_id = ws["id"]
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})
        log_message("Updated repo root .env with FABRIC_WORKSPACE_ID")
    elif not workspace_id:
        workspace_id = input("Enter your Fabric Workspace ID: ").strip()
        if not workspace_id:
            log_message("ERROR: Workspace ID is required (or set FABRIC_CAPACITY_ID to auto-create).")
            sys.exit(1)
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})
        log_message("Updated repo root .env with FABRIC_WORKSPACE_ID")
    else:
        update_root_env({"FABRIC_WORKSPACE_ID": workspace_id})
        log_message("Updated repo root .env with FABRIC_WORKSPACE_ID")

    log_message(f"Workspace ID: {workspace_id}")
    log_message(f"Lakehouse Name: {LAKEHOUSE_NAME}")
    log_message(f"Ontology Name: {FABRIC_ONTOLOGY_NAME}")
    log_message(f"Tenant ID: {FABRIC_TENANT_ID or '(default credential tenant)'}")
    log_message(f"Include Embeddings: {INCLUDE_EMBEDDINGS}")

    # Add lab user as Admin so they can see and use the workspace
    if FABRIC_LAB_USER_OID:
        add_workspace_member(workspace_id, FABRIC_LAB_USER_OID, FABRIC_LAB_USER_UPN, "Admin")
    elif FABRIC_LAB_USER_UPN:
        log_message("WARNING: No object ID for lab user, cannot add to workspace")

    try:
        total_steps = 6 if CREATE_ONTOLOGY else 5

        # Step 1: Create Lakehouse
        log_message(f"\n[1/{total_steps}] Creating Lakehouse")
        lakehouse = create_lakehouse(workspace_id, LAKEHOUSE_NAME)
        lakehouse_id = lakehouse["id"]

        # Step 2: Get lakehouse properties
        log_message(f"\n[2/{total_steps}] Getting Lakehouse Properties")
        props = get_lakehouse_properties(workspace_id, lakehouse_id)
        onelake_files = props.get("properties", {}).get("one_lake_files_path", "")
        log_message(f"OneLake Files Path: {onelake_files}")

        # Step 3: Download and process data
        log_message(f"\n[3/{total_steps}] Downloading and Processing Dataset")
        product_data = download_json("product_data.json")
        reference_data = download_json("reference_data.json")

        products = flatten_products(product_data)
        stores = flatten_stores(reference_data)
        inventory = generate_inventory(products, stores)
        suppliers = generate_suppliers()
        product_suppliers = generate_product_suppliers(products, suppliers)
        validate_retail_graph(
            products, stores, inventory, suppliers, product_suppliers
        )

        tables = {
            "products": (products, "products.csv"),
            "categories": (flatten_categories(product_data), "categories.csv"),
            "stores": (stores, "stores.csv"),
            "inventory": (inventory, "inventory.csv"),
            "suppliers": (suppliers, "suppliers.csv"),
            "product_suppliers": (
                product_suppliers,
                "product_suppliers.csv",
            ),
            "year_weights": (flatten_year_weights(reference_data), "year_weights.csv"),
        }

        for table_name, (rows, _) in tables.items():
            log_message(f"  {table_name}: {len(rows)} rows")

        # Step 4: Upload CSVs to OneLake
        log_message(f"\n[4/{total_steps}] Uploading CSVs to OneLake")
        for table_name, (rows, filename) in tables.items():
            csv_data = to_csv_bytes(rows)
            upload_to_onelake(workspace_id, lakehouse_id, filename, csv_data)

        # Step 5: Load tables
        log_message(f"\n[5/{total_steps}] Loading Delta Tables")
        results = {}
        for table_name, (_, filename) in tables.items():
            results[table_name] = load_table(
                workspace_id, lakehouse_id, table_name, filename
            )

        # Summary
        log_message("\n" + "=" * 60)
        log_message("SUMMARY")
        log_message("=" * 60)
        log_message(f"Lakehouse: {LAKEHOUSE_NAME} ({lakehouse_id})")
        log_message(f"Workspace: {workspace_id}")
        log_message("Tables loaded:")
        for table_name in tables:
            success = results.get(table_name, False)
            status = "SUCCESS" if success else "FAILED"
            log_message(f"  {status}: {table_name}")
        table_success = set(results) == set(tables) and all(results.values())

        ontology = None
        ontology_success = True
        if CREATE_ONTOLOGY and table_success:
            log_message(f"\n[6/{total_steps}] Creating Fabric IQ Ontology")
            ontology = create_or_get_ontology(workspace_id, FABRIC_ONTOLOGY_NAME)
            ontology_success = update_ontology_definition(
                workspace_id, ontology["id"], lakehouse_id
            )
            status = "SUCCESS" if ontology_success else "FAILED"
            log_message(f"  {status}: {FABRIC_ONTOLOGY_NAME} ({ontology['id']})")
        elif CREATE_ONTOLOGY:
            ontology_success = False
            log_message("  FAILED: Skipped ontology setup because table loading failed.")

        log_message("=" * 60)

        if table_success and ontology_success:
            log_message("\nAll tables and ontology setup completed successfully!")
            if ontology:
                log_message(f"Ontology: {FABRIC_ONTOLOGY_NAME} ({ontology['id']})")
                ontology_ui_url = get_ontology_ui_url(workspace_id, ontology["id"])
                log_message(f"Fabric UI: {ontology_ui_url}")
                ontology_mcp_url = get_ontology_mcp_url(
                    workspace_id, ontology["id"]
                )
                log_message(f"Ontology MCP: {ontology_mcp_url}")
                update_root_env(
                    {
                        "FABRIC_ONTOLOGY_ID": ontology["id"],
                        "FABRIC_ONTOLOGY_UI_URL": ontology_ui_url,
                        "FABRIC_ONTOLOGY_MCP_URL": ontology_mcp_url,
                    }
                )
                log_message(
                    "Updated repo root .env with FABRIC_ONTOLOGY_ID, "
                    "FABRIC_ONTOLOGY_UI_URL, and FABRIC_ONTOLOGY_MCP_URL"
                )
            return True
        else:
            log_message("\nWARNING: Some tables or ontology setup failed.")
            return False

    except Exception as e:
        log_message(f"ERROR: {type(e).__name__}: {str(e)}")
        log_message(f"Traceback:\n{traceback.format_exc()}")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
