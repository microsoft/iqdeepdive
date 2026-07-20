"""Create the Entra app, OAuth2 connection, and toolbox for the Work IQ agent."""

import argparse
import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import requests
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import WorkIQPreviewToolboxTool
from azure.identity import AzureDeveloperCliCredential
from azure.identity.aio import AzureDeveloperCliCredential as AsyncAzureDeveloperCliCredential
from dotenv import load_dotenv, set_key
from kiota_abstractions.api_error import APIError
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph import GraphServiceClient
from msgraph.generated.applications.applications_request_builder import ApplicationsRequestBuilder
from msgraph.generated.applications.item.add_password.add_password_post_request_body import (
    AddPasswordPostRequestBody,
)
from msgraph.generated.models.application import Application
from msgraph.generated.models.o_auth2_permission_grant import OAuth2PermissionGrant
from msgraph.generated.models.password_credential import PasswordCredential
from msgraph.generated.models.required_resource_access import RequiredResourceAccess
from msgraph.generated.models.resource_access import ResourceAccess
from msgraph.generated.models.service_principal import ServicePrincipal
from msgraph.generated.models.web_application import WebApplication
from msgraph.generated.oauth2_permission_grants.oauth2_permission_grants_request_builder import (
    Oauth2PermissionGrantsRequestBuilder,
)

load_dotenv(dotenv_path=".env", override=True)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
WORK_IQ_APP_ID = "fdcc1f02-fc51-4226-8753-f668596af7f7"
WORK_IQ_SCOPE = "WorkIQAgent.Ask"
WORK_IQ_TARGET = "https://workiq.svc.cloud.microsoft/a2a/"


def connection_url(project_id: str, connection_name: str) -> str:
    """Return the ARM URL for a Foundry project connection."""
    return (
        f"https://management.azure.com{project_id}/connections/{connection_name}"
        "?api-version=2025-04-01-preview"
    )


def get_management_headers(tenant_id: str) -> dict[str, str]:
    """Return authenticated ARM request headers."""
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    token = credential.get_token(MANAGEMENT_SCOPE).token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_connection(url: str, headers: dict[str, str]) -> dict | None:
    """Return an existing connection, or None when it has not been created."""
    response = requests.get(url, headers=headers, timeout=120)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def save_application_id(application_id: str) -> None:
    """Persist the non-secret application ID in the azd and local environments."""
    subprocess.run(
        ["azd", "env", "set", "WORK_IQ_ENTRA_APP_ID", application_id],
        check=True,
    )
    ENV_PATH.touch()
    set_key(ENV_PATH, "WORK_IQ_ENTRA_APP_ID", application_id, quote_mode="never")


async def get_application(
    graph_client: GraphServiceClient, application_id: str
) -> Application | None:
    """Find an application registration by client ID."""
    query = ApplicationsRequestBuilder.ApplicationsRequestBuilderGetQueryParameters(
        filter=f"appId eq '{application_id}'"
    )
    config = RequestConfiguration[
        ApplicationsRequestBuilder.ApplicationsRequestBuilderGetQueryParameters
    ](query_parameters=query)
    result = await graph_client.applications.get(request_configuration=config)
    return result.value[0] if result and result.value else None


async def get_or_create_work_iq_principal(
    graph_client: GraphServiceClient,
) -> ServicePrincipal:
    """Resolve the tenant's Work IQ principal, creating it when permitted."""
    try:
        principal = await graph_client.service_principals_with_app_id(WORK_IQ_APP_ID).get()
    except APIError as error:
        if error.response_status_code != 404:
            raise
        principal = None
    if principal:
        return principal

    print("Provisioning the Work IQ service principal in this tenant...")
    principal = await graph_client.service_principals.post(
        ServicePrincipal(app_id=WORK_IQ_APP_ID)
    )
    if not principal:
        raise RuntimeError("Microsoft Graph did not return the Work IQ service principal.")
    return principal


def get_work_iq_scope_id(principal: ServicePrincipal) -> uuid.UUID:
    """Return the delegated WorkIQAgent.Ask permission ID."""
    for scope in principal.oauth2_permission_scopes or []:
        if scope.value == WORK_IQ_SCOPE and scope.id:
            return scope.id
    raise RuntimeError(
        "The Work IQ service principal does not expose WorkIQAgent.Ask. "
        "Confirm that Work IQ is enabled in this tenant."
    )


async def get_or_create_application(
    graph_client: GraphServiceClient,
    application_id: str | None,
    scope_id: uuid.UUID,
) -> Application:
    """Create or update the single-tenant Work IQ client application."""
    required_access = [
        RequiredResourceAccess(
            resource_app_id=WORK_IQ_APP_ID,
            resource_access=[ResourceAccess(id=scope_id, type="Scope")],
        )
    ]
    application = await get_application(graph_client, application_id) if application_id else None
    if application:
        if not application.id:
            raise RuntimeError("The Work IQ application has no object ID.")
        await graph_client.applications.by_application_id(application.id).patch(
            Application(required_resource_access=required_access)
        )
        print(f"Reusing Work IQ application {application.app_id}.")
        return application

    print("Creating the single-tenant Work IQ application registration...")
    application = await graph_client.applications.post(
        Application(
            display_name=os.getenv(
                "WORK_IQ_ENTRA_APP_NAME", "Foundry IQ Work IQ Agent"
            ),
            sign_in_audience="AzureADMyOrg",
            required_resource_access=required_access,
        )
    )
    if not application or not application.id or not application.app_id:
        raise RuntimeError("Microsoft Graph did not return the created application IDs.")
    await graph_client.service_principals.post(
        ServicePrincipal(
            app_id=application.app_id,
            display_name=application.display_name,
        )
    )
    save_application_id(application.app_id)
    return application


async def grant_admin_consent(
    graph_client: GraphServiceClient,
    application: Application,
    work_iq_principal: ServicePrincipal,
) -> None:
    """Grant tenant-wide WorkIQAgent.Ask consent to the client application."""
    if not application.app_id or not work_iq_principal.id:
        raise RuntimeError("Application or Work IQ service principal IDs are missing.")
    client_principal = await graph_client.service_principals_with_app_id(
        application.app_id
    ).get()
    if not client_principal or not client_principal.id:
        raise RuntimeError("The Work IQ client service principal could not be resolved.")

    query = Oauth2PermissionGrantsRequestBuilder.Oauth2PermissionGrantsRequestBuilderGetQueryParameters(
        filter=(
            f"clientId eq '{client_principal.id}' and "
            f"resourceId eq '{work_iq_principal.id}'"
        )
    )
    config = RequestConfiguration[
        Oauth2PermissionGrantsRequestBuilder.Oauth2PermissionGrantsRequestBuilderGetQueryParameters
    ](query_parameters=query)
    grants = await graph_client.oauth2_permission_grants.get(request_configuration=config)
    if grants and any(WORK_IQ_SCOPE in (grant.scope or "").split() for grant in grants.value or []):
        print(f"Admin consent for {WORK_IQ_SCOPE} is already granted.")
        return

    try:
        await graph_client.oauth2_permission_grants.post(
            OAuth2PermissionGrant(
                client_id=client_principal.id,
                consent_type="AllPrincipals",
                resource_id=work_iq_principal.id,
                scope=WORK_IQ_SCOPE,
            )
        )
    except APIError as error:
        if error.response_status_code in {401, 403}:
            raise RuntimeError(
                "Tenant-wide Work IQ consent requires an Entra Global Administrator. "
                "Ask an administrator authenticated to this tenant to run:\n\n"
                f"az ad app permission admin-consent --id {application.app_id}"
            ) from error
        raise
    print(f"Granted tenant-wide admin consent for {WORK_IQ_SCOPE}.")


async def add_client_secret(
    graph_client: GraphServiceClient, application: Application
) -> str:
    """Create a client secret and return its one-time value."""
    if not application.id:
        raise RuntimeError("The Work IQ application has no object ID.")
    credential = await graph_client.applications.by_application_id(
        application.id
    ).add_password.post(
        AddPasswordPostRequestBody(
            password_credential=PasswordCredential(
                display_name="Foundry Work IQ connection"
            )
        )
    )
    if not credential or not credential.secret_text:
        raise RuntimeError("Microsoft Graph did not return the client secret value.")
    return credential.secret_text


async def add_redirect_uri(
    graph_client: GraphServiceClient,
    application: Application,
    redirect_uri: str,
) -> None:
    """Add the Foundry OAuth callback as a web redirect URI."""
    if not application.id:
        raise RuntimeError("The Work IQ application has no object ID.")
    current = await graph_client.applications.by_application_id(application.id).get()
    existing = list(current.web.redirect_uris or []) if current and current.web else []
    if redirect_uri in existing:
        print("Foundry OAuth redirect URI is already registered.")
        return
    await graph_client.applications.by_application_id(application.id).patch(
        Application(web=WebApplication(redirect_uris=[*existing, redirect_uri]))
    )
    print("Added the Foundry OAuth redirect URI to the Work IQ application.")


def create_connection(
    url: str,
    headers: dict[str, str],
    connection_name: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Create the Foundry OAuth2 RemoteA2A connection."""
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    payload = {
        "name": connection_name,
        "properties": {
            "authType": "OAuth2",
            "group": "ServicesAndApps",
            "category": "RemoteA2A",
            "expiryTime": None,
            "target": WORK_IQ_TARGET,
            "isSharedToAll": True,
            "sharedUserList": [],
            "TokenUrl": token_url,
            "AuthorizationUrl": (
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
            ),
            "RefreshUrl": token_url,
            "Scopes": [
                "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask",
                "offline_access",
            ],
            "Credentials": {
                "ClientId": client_id,
                "ClientSecret": client_secret,
            },
            "metadata": {"ApiType": "Azure"},
        },
    }
    response = requests.put(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


def create_toolbox(endpoint: str, toolbox_name: str, connection_name: str) -> None:
    """Create and promote a toolbox version containing Work IQ."""
    credential = AzureDeveloperCliCredential(tenant_id=os.environ["AZURE_TENANT_ID"])
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    version = project.toolboxes.create_version(
        name=toolbox_name,
        tools=[
            WorkIQPreviewToolboxTool(
                name="work_iq",
                description=(
                    "Answer questions about the signed-in user's Microsoft 365 mail, "
                    "chats, meetings, and documents."
                ),
                project_connection_id=connection_name,
            )
        ],
        description="Microsoft 365 work context tools for the Work IQ agent.",
    )
    project.toolboxes.update(name=toolbox_name, default_version=version.version)
    print(f"Set toolbox '{toolbox_name}' default version to {version.version}.")


async def apply() -> None:
    """Apply the Work IQ Entra, connection, and toolbox configuration."""
    tenant_id = os.environ["AZURE_TENANT_ID"]
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    project_id = os.environ["AZURE_AI_PROJECT_ID"]
    connection_name = os.getenv("WORK_IQ_CONNECTION_NAME", "work-iq-connection")
    toolbox_name = os.getenv("CUSTOM_FOUNDRY_WORKIQ_TOOLBOX_NAME", "work-iq-tools")
    url = connection_url(project_id, connection_name)
    headers = get_management_headers(tenant_id)
    existing_connection = get_connection(url, headers)

    async with AsyncAzureDeveloperCliCredential(tenant_id=tenant_id) as credential:
        graph_client = GraphServiceClient(credentials=credential, scopes=GRAPH_SCOPES)
        try:
            work_iq_principal = await get_or_create_work_iq_principal(graph_client)
            scope_id = get_work_iq_scope_id(work_iq_principal)
            existing_client_id = None
            if existing_connection:
                existing_client_id = (
                    existing_connection.get("properties", {})
                    .get("Credentials", {})
                    .get("ClientId")
                )
            application = await get_or_create_application(
                graph_client,
                os.getenv("WORK_IQ_ENTRA_APP_ID") or existing_client_id,
                scope_id,
            )
            await grant_admin_consent(graph_client, application, work_iq_principal)

            if existing_connection:
                connection = existing_connection
                print(f"Reusing Foundry connection '{connection_name}'.")
            else:
                if not application.app_id:
                    raise RuntimeError("The Work IQ application has no client ID.")
                client_secret = await add_client_secret(graph_client, application)
                connection = create_connection(
                    url,
                    headers,
                    connection_name,
                    tenant_id,
                    application.app_id,
                    client_secret,
                )
                print(f"Created Foundry connection '{connection_name}'.")

            connection_properties = connection.get("properties", {})
            redirect_uri = connection_properties.get(
                "redirectUrl"
            ) or connection_properties.get("oauthRedirectUrl")
            if not redirect_uri:
                raise RuntimeError("The Foundry connection did not return an OAuth redirect URL.")
            await add_redirect_uri(graph_client, application, redirect_uri)
        except APIError as error:
            if error.response_status_code in {401, 403}:
                raise RuntimeError(
                    "Work IQ setup requires an Entra Global Administrator. Enable the tenant with "
                    f"'az ad sp create --id {WORK_IQ_APP_ID}', then rerun this helper using an "
                    "identity allowed to create app registrations and grant tenant-wide consent."
                ) from error
            raise

    create_toolbox(endpoint, toolbox_name, connection_name)


def dry_run() -> None:
    """Validate required local inputs without changing Entra or Azure resources."""
    required = ("AZURE_TENANT_ID", "FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ID")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    print("Work IQ setup inputs are valid.")
    print(f"Tenant: {os.environ['AZURE_TENANT_ID']}")
    print(f"Target: {WORK_IQ_TARGET}")
    print("No tenant or Azure resources were changed.")


def main() -> None:
    """Parse arguments and run validation or apply the Work IQ configuration."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
    else:
        try:
            asyncio.run(apply())
        except RuntimeError as error:
            raise SystemExit(f"ERROR: {error}") from None


if __name__ == "__main__":
    main()