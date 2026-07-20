"""Probe the microsoft-fabric-api SDK against the configured Fabric workspace.

The default run is read-only. Pass --create-probe to create, read, and delete a
uniquely named temporary lakehouse using only SDK operations.
"""

import argparse
import os
import sys
import warnings
from datetime import UTC, datetime

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

# microsoft-fabric-api 0.1.0b20 emits invalid-escape SyntaxWarnings on Python 3.14.
warnings.filterwarnings(
    "ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*"
)

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.lakehouse.models import (  # noqa: E402
    CreateLakehouseRequest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test whether microsoft-fabric-api can replace Fabric REST calls."
    )
    parser.add_argument(
        "--workspace-id",
        default=os.getenv("FABRIC_WORKSPACE_ID", ""),
        help="Fabric workspace GUID (defaults to FABRIC_WORKSPACE_ID).",
    )
    parser.add_argument(
        "--create-probe",
        action="store_true",
        help="Create, retrieve, and delete a temporary lakehouse.",
    )
    return parser.parse_args()


def display_name(item: object) -> str:
    return str(getattr(item, "display_name", "(unnamed)"))


def item_id(item: object) -> str:
    return str(getattr(item, "id", ""))


def table_name(table: object) -> str:
    return str(getattr(table, "name", "(unnamed)"))


def print_capability_report(client: FabricClient) -> None:
    checks = {
        "List/create workspaces": hasattr(client.core.workspaces, "create_workspace"),
        "Add workspace members": hasattr(
            client.core.workspaces, "add_workspace_role_assignment"
        ),
        "Lakehouse CRUD": hasattr(client.lakehouse.items, "begin_create_lakehouse"),
        "Load Delta tables": hasattr(client.lakehouse.tables, "begin_load_table"),
        "Ontology CRUD": hasattr(client.ontology.items, "begin_create_ontology"),
        "Update ontology definition": hasattr(
            client.ontology.items, "begin_update_ontology_definition"
        ),
    }

    print("\nSDK replacement coverage")
    for operation, supported in checks.items():
        print(f"  {'YES' if supported else 'NO ':3}  {operation}")
    print("  NO   Upload files to OneLake (keep azure-storage-file-datalake)")


def inspect_workspace(client: FabricClient, workspace_id: str) -> None:
    workspace = client.core.workspaces.get_workspace(workspace_id)
    print(f"\nWorkspace: {display_name(workspace)} ({item_id(workspace)})")

    lakehouses = list(client.lakehouse.items.list_lakehouses(workspace_id))
    print(f"Lakehouses ({len(lakehouses)}):")
    for lakehouse in lakehouses:
        print(f"  {display_name(lakehouse)} ({item_id(lakehouse)})")

    configured_name = os.getenv("LAKEHOUSE_NAME", "ContosoDIYLakehouse")
    configured_lakehouse = next(
        (item for item in lakehouses if display_name(item) == configured_name), None
    )
    if configured_lakehouse:
        tables = list(
            client.lakehouse.tables.list_tables(
                workspace_id, item_id(configured_lakehouse)
            )
        )
        print(f"Tables in {configured_name} ({len(tables)}):")
        for table in tables:
            print(f"  {table_name(table)}")
    else:
        print(f"Configured lakehouse not found: {configured_name}")

    ontologies = list(client.ontology.items.list_ontologies(workspace_id))
    print(f"Ontologies ({len(ontologies)}):")
    for ontology in ontologies:
        print(f"  {display_name(ontology)} ({item_id(ontology)})")


def exercise_lakehouse_create(client: FabricClient, workspace_id: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    probe_name = f"FabricSdkProbe-{timestamp}"
    probe_id = ""

    print(f"\nCreating temporary lakehouse: {probe_name}")
    try:
        poller = client.lakehouse.items.begin_create_lakehouse(
            workspace_id,
            CreateLakehouseRequest(
                display_name=probe_name,
                description="Temporary microsoft-fabric-api SDK probe.",
            ),
        )
        lakehouse = poller.result()
        probe_id = item_id(lakehouse)
        retrieved = client.lakehouse.items.get_lakehouse(workspace_id, probe_id)
        print(f"Created and retrieved: {display_name(retrieved)} ({probe_id})")
    finally:
        if probe_id:
            client.lakehouse.items.delete_lakehouse(workspace_id, probe_id)
            print(f"Deleted temporary lakehouse: {probe_id}")


def main() -> int:
    load_dotenv(override=True)
    args = parse_args()
    tenant_id = os.getenv("FABRIC_TENANT_ID", "").strip()
    if not tenant_id:
        print("ERROR: FABRIC_TENANT_ID is required for authentication.", file=sys.stderr)
        return 2

    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    client = FabricClient(credential)

    print_capability_report(client)

    try:
        if args.workspace_id:
            inspect_workspace(client, args.workspace_id)
            if args.create_probe:
                exercise_lakehouse_create(client, args.workspace_id)
        else:
            workspaces = list(client.core.workspaces.list_workspaces())
            print(f"\nAccessible workspaces ({len(workspaces)}):")
            for workspace in workspaces:
                print(f"  {display_name(workspace)} ({item_id(workspace)})")
            if args.create_probe:
                print("ERROR: --create-probe requires --workspace-id or FABRIC_WORKSPACE_ID.")
                return 2
    except (ClientAuthenticationError, HttpResponseError) as error:
        print(f"\nSDK request failed: {error}", file=sys.stderr)
        return 1

    print("\nSDK probe completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
