"""Create the Entra app and OAuth2 connection that let a Foundry agent query a Fabric ontology.

Fabric IQ authenticates with Microsoft Entra delegated authentication (On-Behalf-Of). Every
request runs as the signed-in user, and application-only authentication is not supported at
all. A `ProjectManagedIdentity` connection therefore cannot reach an ontology: it fails with
`Failed to translate NL query to ontology query` no matter which tool type is used, and
fronting the ontology with a data agent does not help because the data agent reads the
ontology as its caller. Promoting the identity to workspace Admin changes nothing, because
the limitation is the authentication mode rather than a Fabric role.

The supported route for an ontology item is a bring-your-own Entra application holding the
Power BI delegated permissions `Item.Execute.All` and `Item.Read.All`, wired to Foundry as an
OAuth2 connection. This script performs that setup end to end:

1. registers or reuses a single-tenant application with those delegated permissions,
2. grants tenant-wide admin consent when the signed-in account can,
3. issues a client secret,
4. creates the OAuth2 `RemoteTool` connection targeting the ontology MCP endpoint, and
5. registers the connection's OAuth callback back on the application.

The Entra plumbing deliberately reuses `infra/create-toolbox-workiq.py`, which already
establishes this pattern for Work IQ.

Prerequisite:
  infra/create-research-ontology.py   creates ResearchLiteratureOntology

Usage:
  uv run python infra/create-fabriciq-ontology-connection.py
"""

import argparse
import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import requests
from azure.identity.aio import AzureDeveloperCliCredential as AsyncAzureDeveloperCliCredential
from dotenv import load_dotenv, set_key
from kiota_abstractions.api_error import APIError
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph import GraphServiceClient
from msgraph.generated.applications.item.add_password.add_password_post_request_body import (
    AddPasswordPostRequestBody,
)
from msgraph.generated.models.application import Application
from msgraph.generated.models.o_auth2_permission_grant import OAuth2PermissionGrant
from msgraph.generated.models.password_credential import PasswordCredential
from msgraph.generated.models.required_resource_access import RequiredResourceAccess
from msgraph.generated.models.resource_access import ResourceAccess
from msgraph.generated.models.service_principal import ServicePrincipal
from msgraph.generated.oauth2_permission_grants.oauth2_permission_grants_request_builder import (
    Oauth2PermissionGrantsRequestBuilder,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"

load_dotenv(ENV_PATH, override=True)

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# The Power BI Service first-party application. Fabric item permissions are published here.
POWER_BI_APP_ID = "00000009-0000-0000-c000-000000000000"
POWER_BI_RESOURCE = "https://analysis.windows.net/powerbi/api"
# The ontology endpoint rejects a token that carries only the documented `Item.Read.All`:
# it answers 401 with `Required scopes: Item.ReadWrite.All and Item.Execute.All`. Trust the
# endpoint over the documentation here.
ONTOLOGY_SCOPES = ("Item.ReadWrite.All", "Item.Execute.All")

CONNECTION_NAME = os.getenv(
    "FABRIC_ONTOLOGY_CONNECTION_NAME", "fabriciq-ontology-connection"
)
APP_NAME = os.getenv("FABRIC_ONTOLOGY_ENTRA_APP_NAME", "Foundry IQ Fabric Ontology")
APP_ID_ENV_KEY = "FABRIC_ONTOLOGY_ENTRA_APP_ID"


def load_workiq_module():
    """Import the Work IQ setup script to reuse its Entra and ARM helpers.

    Its filename is not a valid Python identifier, so it is loaded by path. Reusing it keeps
    one definition of how a Foundry connection URL is built, how ARM headers are minted, how
    an application is looked up by client ID, and how an OAuth callback is registered.
    """
    module_path = Path(__file__).with_name("create-toolbox-workiq.py")
    spec = importlib.util.spec_from_file_location("workiq_entra_base", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    """Parse overrides for the connection and its Entra application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-name", default=CONNECTION_NAME)
    parser.add_argument("--app-name", default=APP_NAME)
    parser.add_argument(
        "--ontology-mcp-url",
        default=os.getenv("FABRIC_RESEARCH_ONTOLOGY_MCP_URL", "").strip().strip("'"),
        help="Ontology MCP endpoint to target. Defaults to the research ontology.",
    )
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help="Issue a new client secret and rewrite the connection even if it exists.",
    )
    return parser.parse_args()


async def get_power_bi_principal(graph_client: GraphServiceClient) -> ServicePrincipal:
    """Resolve the tenant's Power BI Service principal.

    Every tenant with Fabric enabled already has this first-party principal, so unlike the
    Work IQ setup there is nothing to provision here.
    """
    principal = await graph_client.service_principals_with_app_id(POWER_BI_APP_ID).get()
    if not principal:
        raise RuntimeError(
            "The Power BI Service principal is not present in this tenant. "
            "Confirm Fabric is enabled before creating the ontology connection."
        )
    return principal


def resolve_scope_ids(principal: ServicePrincipal) -> dict[str, uuid.UUID]:
    """Return the delegated permission IDs for the ontology scopes."""
    published = {
        scope.value: scope.id
        for scope in principal.oauth2_permission_scopes or []
        if scope.value and scope.id
    }
    missing = [name for name in ONTOLOGY_SCOPES if name not in published]
    if missing:
        raise RuntimeError(
            f"The Power BI Service principal does not publish {', '.join(missing)}."
        )
    return {name: published[name] for name in ONTOLOGY_SCOPES}


async def get_or_create_application(
    base,
    graph_client: GraphServiceClient,
    app_name: str,
    application_id: str | None,
    scope_ids: dict[str, uuid.UUID],
) -> Application:
    """Create or update the single-tenant application that fronts the ontology."""
    required_access = [
        RequiredResourceAccess(
            resource_app_id=POWER_BI_APP_ID,
            resource_access=[
                ResourceAccess(id=scope_id, type="Scope")
                for scope_id in scope_ids.values()
            ],
        )
    ]
    application = (
        await base.get_application(graph_client, application_id)
        if application_id
        else None
    )
    if application:
        if not application.id:
            raise RuntimeError("The ontology application has no object ID.")
        await graph_client.applications.by_application_id(application.id).patch(
            Application(required_resource_access=required_access)
        )
        print(f"Reusing ontology application {application.app_id}.")
        return application

    print(f"Creating the single-tenant application '{app_name}'...")
    application = await graph_client.applications.post(
        Application(
            display_name=app_name,
            sign_in_audience="AzureADMyOrg",
            required_resource_access=required_access,
            service_management_reference=os.getenv("AZURE_SERVICE_MANAGEMENT_REFERENCE")
            or None,
        )
    )
    if not application or not application.id or not application.app_id:
        raise RuntimeError("Microsoft Graph did not return the created application IDs.")
    await graph_client.service_principals.post(
        ServicePrincipal(
            app_id=application.app_id, display_name=application.display_name
        )
    )
    ENV_PATH.touch()
    set_key(ENV_PATH, APP_ID_ENV_KEY, application.app_id, quote_mode="never")
    print(f"Created application {application.app_id}.")
    return application


async def grant_admin_consent(
    graph_client: GraphServiceClient,
    application: Application,
    power_bi_principal: ServicePrincipal,
) -> None:
    """Grant tenant-wide consent for the ontology scopes.

    Both scopes are user-consentable, so a non-administrator can still complete the flow
    interactively on first use. A missing administrator role is reported rather than raised.
    """
    if not application.app_id or not power_bi_principal.id:
        raise RuntimeError("Application or Power BI principal IDs are missing.")
    client_principal = await graph_client.service_principals_with_app_id(
        application.app_id
    ).get()
    if not client_principal or not client_principal.id:
        raise RuntimeError("The ontology client service principal could not be resolved.")

    query = Oauth2PermissionGrantsRequestBuilder.Oauth2PermissionGrantsRequestBuilderGetQueryParameters(
        filter=(
            f"clientId eq '{client_principal.id}' and "
            f"resourceId eq '{power_bi_principal.id}'"
        )
    )
    config = RequestConfiguration[
        Oauth2PermissionGrantsRequestBuilder.Oauth2PermissionGrantsRequestBuilderGetQueryParameters
    ](query_parameters=query)
    grants = await graph_client.oauth2_permission_grants.get(request_configuration=config)
    wanted = {*ONTOLOGY_SCOPES, "offline_access"}
    for grant in (grants.value if grants else None) or []:
        current = set((grant.scope or "").split())
        if wanted.issubset(current):
            print("Admin consent for the ontology scopes is already granted.")
            return
        if grant.id:
            # A grant already exists for this client and resource pair, so Graph rejects a
            # second one with Request_MultipleObjectsWithSameKeyValue. Widen the existing
            # grant instead, keeping any scopes another caller added.
            merged = " ".join(sorted(current | wanted))
            await graph_client.oauth2_permission_grants.by_o_auth2_permission_grant_id(
                grant.id
            ).patch(OAuth2PermissionGrant(scope=merged))
            print(f"Updated tenant-wide admin consent to: {merged}")
            return

    try:
        await graph_client.oauth2_permission_grants.post(
            OAuth2PermissionGrant(
                client_id=client_principal.id,
                consent_type="AllPrincipals",
                resource_id=power_bi_principal.id,
                scope=" ".join(sorted(wanted)),
            )
        )
    except APIError as error:
        if error.response_status_code in {401, 403}:
            print(
                "Skipping tenant-wide consent: the signed-in account is not an Entra "
                "Global Administrator. Each caller will be asked to consent the first "
                "time the agent calls the ontology."
            )
            return
        raise
    print("Granted tenant-wide admin consent for the ontology scopes.")


async def add_client_secret(
    graph_client: GraphServiceClient, application: Application
) -> str:
    """Create a client secret and return its one-time value."""
    if not application.id:
        raise RuntimeError("The ontology application has no object ID.")
    credential = await graph_client.applications.by_application_id(
        application.id
    ).add_password.post(
        AddPasswordPostRequestBody(
            password_credential=PasswordCredential(
                display_name="Foundry Fabric ontology connection"
            )
        )
    )
    if not credential or not credential.secret_text:
        raise RuntimeError("Microsoft Graph did not return the client secret value.")
    return credential.secret_text


def create_connection(
    url: str,
    headers: dict[str, str],
    connection_name: str,
    tenant_id: str,
    target: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Create the Foundry OAuth2 RemoteTool connection for the ontology endpoint."""
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "name": connection_name,
        "properties": {
            "authType": "OAuth2",
            "group": "ServicesAndApps",
            "category": "RemoteTool",
            "expiryTime": None,
            "target": target,
            "isSharedToAll": True,
            "sharedUserList": [],
            "TokenUrl": token_url,
            "AuthorizationUrl": (
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
            ),
            "RefreshUrl": token_url,
            "Scopes": [
                *(f"{POWER_BI_RESOURCE}/{scope}" for scope in ONTOLOGY_SCOPES),
                "offline_access",
            ],
            "Credentials": {"ClientId": client_id, "ClientSecret": client_secret},
            "metadata": {"ApiType": "Azure"},
        },
    }
    response = requests.put(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


async def apply(args: argparse.Namespace) -> None:
    """Create the Entra application and the Foundry OAuth2 ontology connection."""
    base = load_workiq_module()
    load_dotenv(ENV_PATH, override=True)

    if not args.ontology_mcp_url:
        raise RuntimeError(
            "No ontology MCP endpoint. Run infra/create-research-ontology.py first, "
            "or pass --ontology-mcp-url."
        )

    tenant_id = os.environ["AZURE_TENANT_ID"]
    project_id = os.environ["AZURE_AI_PROJECT_ID"]
    url = base.connection_url(project_id, args.connection_name)
    headers = base.get_management_headers(tenant_id)
    existing = base.get_connection(url, headers)

    print(f"Target: {args.ontology_mcp_url}")

    async with AsyncAzureDeveloperCliCredential(tenant_id=tenant_id) as credential:
        graph_client = GraphServiceClient(credentials=credential, scopes=GRAPH_SCOPES)
        power_bi = await get_power_bi_principal(graph_client)
        scope_ids = resolve_scope_ids(power_bi)

        existing_client_id = (
            (existing or {}).get("properties", {}).get("Credentials", {}).get("ClientId")
        )
        application = await get_or_create_application(
            base,
            graph_client,
            args.app_name,
            os.getenv(APP_ID_ENV_KEY) or existing_client_id,
            scope_ids,
        )
        await grant_admin_consent(graph_client, application, power_bi)

        if existing and not args.rotate_secret:
            connection = existing
            print(f"Reusing Foundry connection '{args.connection_name}'.")
        else:
            if not application.app_id:
                raise RuntimeError("The ontology application has no client ID.")
            secret = await add_client_secret(graph_client, application)
            connection = create_connection(
                url,
                headers,
                args.connection_name,
                tenant_id,
                args.ontology_mcp_url,
                application.app_id,
                secret,
            )
            print(f"Created Foundry connection '{args.connection_name}'.")

        properties = connection.get("properties", {})
        redirect_uri = properties.get("redirectUrl") or properties.get(
            "oauthRedirectUrl"
        )
        if not redirect_uri:
            raise RuntimeError(
                "The Foundry connection did not return an OAuth redirect URL."
            )
        await base.add_redirect_uri(graph_client, application, redirect_uri)

    set_key(ENV_PATH, "FABRIC_ONTOLOGY_CONNECTION_NAME", args.connection_name, quote_mode="never")
    print()
    print(f"Connection:  {args.connection_name}")
    print(f"Client ID:   {application.app_id}")
    print(f"Redirect:    {redirect_uri}")
    print("The first agent call returns a consent URL; complete it once in a browser.")


def main() -> None:
    """Entry point."""
    asyncio.run(apply(parse_args()))


if __name__ == "__main__":
    main()
