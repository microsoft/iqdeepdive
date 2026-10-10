"""Provision the Work IQ Microsoft Entra app used by the Azure AI Search Work IQ knowledge source.

For the 2026-08-01-preview Work IQ knowledge source, Azure AI Search authenticates as a
customer-owned Entra app through a federated credential that trusts the search service managed
identity. This script creates (or reuses) that app, exposes the required delegated scope, grants
tenant-wide admin consent, and creates a federated credential for the search service identity.

It writes these non-secret values to .env for the notebook and knowledge base helper:

- WORK_IQ_SEARCH_ENTRA_APP_ID: application (client) ID
- WORK_IQ_SEARCH_ENTRA_TENANT_ID: directory (tenant) ID of the app
- WORK_IQ_SEARCH_FEDERATED_CREDENTIAL_ID: object ID of the federated credential

No client secret is created. The signed-in user always authorizes retrieval, so Work IQ honors
Microsoft 365 permissions. See:
https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-work-iq

The app is also reused by `infra/create-toolbox-workiq.py` for the OAuth2 `RemoteA2A` connection, which
adds a client secret and the Foundry callback URI to it. The secret goes straight to the Foundry
connection and is not written to `.env`.
"""

import argparse
import os
import time
import uuid
from pathlib import Path

import requests
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, ChainedTokenCredential
from dotenv import load_dotenv, set_key

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Work IQ first-party application and its delegated permission.
WORK_IQ_APP_ID = "fdcc1f02-fc51-4226-8753-f668596af7f7"
WORK_IQ_SCOPE = "WorkIQAgent.Ask"
WORK_IQ_SCOPE_ID_FALLBACK = "0b1715fd-f4bf-4c63-b16d-5be31f9847c2"

# Scope the notebook requests through the customer-owned app.
CLIENT_SCOPE = "access_as_user"
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"

# Retry Graph throttling and transient errors, and wait for newly created objects to replicate.
MAX_RETRIES = 6
PROPAGATION_TIMEOUT_SECONDS = 600
PROPAGATION_INTERVAL_SECONDS = 15
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

PERMISSION_HINT = (
    "The identity needs the Cloud Application Administrator role, or the Microsoft Graph "
    "application permissions Application.ReadWrite.All, DelegatedPermissionGrant.ReadWrite.All, "
    "and Directory.Read.All with admin consent."
)

load_dotenv(dotenv_path=ENV_PATH, override=True)


def require_env(name: str) -> str:
    """Return a required environment setting."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required to provision the Work IQ Entra app.")
    return value


def create_credential(tenant_id: str) -> TokenCredential:
    """Resolve a Graph credential from a signed-in Azure CLI session, then azd."""
    return ChainedTokenCredential(
        AzureCliCredential(tenant_id=tenant_id),
        AzureDeveloperCliCredential(tenant_id=tenant_id),
    )


class GraphNotReadyError(RuntimeError):
    """A Graph call failed because a just-created object has not replicated yet."""


class GraphClient:
    """Minimal Microsoft Graph REST client with throttling and transient-error retries."""

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token(GRAPH_SCOPE).token
        return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def request(self, method: str, path: str, payload: dict | None = None) -> requests.Response:
        """Send a request, retrying throttling (429) and transient server errors."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.request(
                    method,
                    f"{GRAPH_BASE}{path}",
                    headers=self._headers(),
                    json=payload,
                    timeout=120,
                )
            except requests.RequestException:
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(min(2**attempt, 30))
                continue
            if response.status_code not in RETRY_STATUS_CODES or attempt == MAX_RETRIES:
                return response
            delay = response.headers.get("Retry-After", "")
            time.sleep(int(delay) if delay.isdigit() else min(2**attempt, 30))
        return response

    def get(self, path: str) -> requests.Response:
        return self.request("GET", path)

    def post(self, path: str, payload: dict) -> dict:
        response = self.request("POST", path, payload)
        _raise_for_graph(response)
        return response.json() if response.content else {}

    def patch(self, path: str, payload: dict) -> None:
        _raise_for_graph(self.request("PATCH", path, payload))


def wait_until_ready(action: str, call):
    """Retry a call while a newly created directory object is still replicating.

    Microsoft Entra is eventually consistent, so a just-created application or service
    principal can return 400 or 404 to follow-up calls for several minutes.
    """
    deadline = time.monotonic() + PROPAGATION_TIMEOUT_SECONDS
    while True:
        try:
            return call()
        except GraphNotReadyError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{action} did not succeed after {PROPAGATION_TIMEOUT_SECONDS} seconds. "
                    f"If this persists, check permissions. {PERMISSION_HINT} Last error: {error}"
                ) from None
            print(f"Waiting for Microsoft Entra to replicate ({action})...")
            time.sleep(PROPAGATION_INTERVAL_SECONDS)


def _raise_for_graph(response: requests.Response) -> None:
    """Raise a helpful error, calling out the permissions Work IQ setup requires."""
    if response.ok:
        return
    if response.status_code in (401, 403):
        raise RuntimeError(
            "Work IQ Entra setup was denied. The identity needs to create app registrations, "
            "grant tenant-wide admin consent for WorkIQAgent.Ask, and create federated "
            f"credentials. {PERMISSION_HINT} "
            f"Underlying error: {response.status_code} {response.text}"
        )
    if response.status_code in (400, 404):
        raise GraphNotReadyError(f"Microsoft Graph error {response.status_code}: {response.text}")
    raise RuntimeError(f"Microsoft Graph error {response.status_code}: {response.text}")


def get_or_create_work_iq_sp(graph: GraphClient) -> dict:
    """Resolve the Work IQ service principal, provisioning it when it does not exist."""
    response = graph.get(f"/servicePrincipals(appId='{WORK_IQ_APP_ID}')")
    if response.status_code == 200:
        return response.json()
    if response.status_code != 404:
        _raise_for_graph(response)
    print("Provisioning the Work IQ service principal in this tenant...")
    return graph.post("/servicePrincipals", {"appId": WORK_IQ_APP_ID})


def work_iq_scope_id(work_iq_sp: dict) -> str:
    """Return the WorkIQAgent.Ask delegated permission ID."""
    for scope in work_iq_sp.get("oauth2PermissionScopes", []):
        if scope.get("value") == WORK_IQ_SCOPE and scope.get("id"):
            return scope["id"]
    return WORK_IQ_SCOPE_ID_FALLBACK


def find_application(graph: GraphClient, app_id: str, display_name: str) -> dict | None:
    """Return the app by client ID, or by its unique display name."""
    if app_id.strip():
        response = graph.get(f"/applications(appId='{app_id}')")
        if response.status_code == 200:
            return response.json()
        if response.status_code != 404:
            _raise_for_graph(response)
    # Fall back to the display name so a rerun reuses the app instead of creating a duplicate.
    escaped = display_name.replace("'", "''")
    response = graph.get(f"/applications?$filter=displayName eq '{escaped}'")
    _raise_for_graph(response)
    matches = response.json().get("value", [])
    return matches[0] if matches else None


def create_application(graph: GraphClient, display_name: str, work_iq_scope: str) -> dict:
    """Create the single-tenant Work IQ client application with the access_as_user scope."""
    exposed_scope = {
        "id": str(uuid.uuid4()),
        "adminConsentDisplayName": "Access Azure AI Search as the signed-in user",
        "adminConsentDescription": ("Allow Azure AI Search to call Work IQ on behalf of the signed-in user."),
        "userConsentDisplayName": "Access Work IQ on your behalf",
        "userConsentDescription": ("Allow Azure AI Search to query Work IQ using your Microsoft 365 permissions."),
        "value": CLIENT_SCOPE,
        "type": "User",
        "isEnabled": True,
    }
    print(f"Creating the single-tenant Work IQ application '{display_name}'...")
    return graph.post(
        "/applications",
        {
            "displayName": display_name,
            "signInAudience": "AzureADMyOrg",
            "isFallbackPublicClient": True,
            "publicClient": {"redirectUris": ["http://localhost"]},
            "requiredResourceAccess": [
                {
                    "resourceAppId": WORK_IQ_APP_ID,
                    "resourceAccess": [{"id": work_iq_scope, "type": "Scope"}],
                }
            ],
            "api": {"oauth2PermissionScopes": [exposed_scope]},
        },
    )


def configure_application(graph: GraphClient, application: dict) -> None:
    """Set the identifier URI and pre-authorize the app as its own client.

    This lets the notebook request api://<app-id>/access_as_user without an extra consent
    prompt. It is idempotent, so a rerun completes an app that a previous run left unfinished.
    """
    app_id = application["appId"]
    scopes = (application.get("api") or {}).get("oauth2PermissionScopes") or []
    scope = next((item for item in scopes if item.get("value") == CLIENT_SCOPE), None)
    if scope is None:
        raise RuntimeError(f"Application {app_id} does not expose the {CLIENT_SCOPE} scope.")
    identifier = f"api://{app_id}"
    authorized = (application.get("api") or {}).get("preAuthorizedApplications") or []
    if identifier in (application.get("identifierUris") or []) and any(
        item.get("appId") == app_id for item in authorized
    ):
        return
    wait_until_ready(
        "configure the application",
        lambda: graph.patch(
            f"/applications/{application['id']}",
            {
                "identifierUris": [identifier],
                "api": {
                    "oauth2PermissionScopes": scopes,
                    "preAuthorizedApplications": [{"appId": app_id, "delegatedPermissionIds": [scope["id"]]}],
                },
            },
        ),
    )


def ensure_service_principal(graph: GraphClient, app_id: str) -> dict:
    """Return the app's service principal, creating it when needed."""

    def attempt() -> dict:
        response = graph.get(f"/servicePrincipals(appId='{app_id}')")
        if response.status_code == 200:
            return response.json()
        if response.status_code != 404:
            _raise_for_graph(response)
        return graph.post("/servicePrincipals", {"appId": app_id})

    return wait_until_ready("create the service principal", attempt)


def grant_admin_consent(graph: GraphClient, client_sp_id: str, work_iq_sp_id: str) -> None:
    """Grant tenant-wide WorkIQAgent.Ask consent to the client service principal."""

    def attempt() -> None:
        existing = graph.get(
            f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp_id}' and resourceId eq '{work_iq_sp_id}'"
        )
        _raise_for_graph(existing)
        for grant in existing.json().get("value", []):
            if WORK_IQ_SCOPE in (grant.get("scope") or "").split():
                print(f"Admin consent for {WORK_IQ_SCOPE} is already granted.")
                return
        graph.post(
            "/oauth2PermissionGrants",
            {
                "clientId": client_sp_id,
                "consentType": "AllPrincipals",
                "resourceId": work_iq_sp_id,
                "scope": WORK_IQ_SCOPE,
            },
        )
        print(f"Granted tenant-wide admin consent for {WORK_IQ_SCOPE}.")

    wait_until_ready("grant admin consent", attempt)


def create_federated_credential(
    graph: GraphClient,
    app_object_id: str,
    credential_name: str,
    tenant_id: str,
    subject: str,
) -> str:
    """Create (or reuse) a federated credential trusting the search service identity."""

    def attempt() -> str:
        existing = graph.get(f"/applications/{app_object_id}/federatedIdentityCredentials")
        _raise_for_graph(existing)
        for credential in existing.json().get("value", []):
            if credential.get("subject") == subject and credential.get("id"):
                print("Reusing the existing federated credential for the search identity.")
                return credential["id"]
        created = graph.post(
            f"/applications/{app_object_id}/federatedIdentityCredentials",
            {
                "name": credential_name,
                "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                "subject": subject,
                "audiences": [TOKEN_EXCHANGE_AUDIENCE],
            },
        )
        return created["id"]

    return wait_until_ready("create the federated credential", attempt)


def save_env(app_id: str, tenant_id: str, federated_credential_id: str) -> None:
    """Persist the non-secret Work IQ values to .env."""
    ENV_PATH.touch()
    set_key(ENV_PATH, "WORK_IQ_SEARCH_ENTRA_APP_ID", app_id, quote_mode="never")
    set_key(ENV_PATH, "WORK_IQ_SEARCH_ENTRA_TENANT_ID", tenant_id, quote_mode="never")
    set_key(
        ENV_PATH,
        "WORK_IQ_SEARCH_FEDERATED_CREDENTIAL_ID",
        federated_credential_id,
        quote_mode="never",
    )


def apply() -> None:
    """Provision the Work IQ Entra app and federated credential."""
    tenant_id = require_env("AZURE_TENANT_ID")
    search_principal_id = require_env("SEARCH_SERVICE_PRINCIPAL_ID")
    search_service_name = require_env("AZURE_AI_SEARCH_SERVICE_NAME")
    display_name = os.getenv("WORK_IQ_SEARCH_ENTRA_APP_NAME", f"IQDeepDive-WorkIQ-{search_service_name}")

    graph = GraphClient(create_credential(tenant_id))

    work_iq_sp = get_or_create_work_iq_sp(graph)
    scope_id = work_iq_scope_id(work_iq_sp)

    application = find_application(graph, os.getenv("WORK_IQ_SEARCH_ENTRA_APP_ID", ""), display_name)
    if application:
        print(f"Reusing Work IQ application {application['appId']}.")
    else:
        application = create_application(graph, display_name, scope_id)
    configure_application(graph, application)

    client_sp = ensure_service_principal(graph, application["appId"])
    grant_admin_consent(graph, client_sp["id"], work_iq_sp["id"])

    federated_credential_id = create_federated_credential(
        graph,
        application["id"],
        f"{search_service_name}-identity",
        tenant_id,
        search_principal_id,
    )

    save_env(application["appId"], tenant_id, federated_credential_id)
    print("Work IQ Entra app configured.")
    print(f"  WORK_IQ_SEARCH_ENTRA_APP_ID={application['appId']}")
    print(f"  WORK_IQ_SEARCH_FEDERATED_CREDENTIAL_ID={federated_credential_id}")


def dry_run() -> None:
    """Validate required inputs without changing Entra resources."""
    required = ("AZURE_TENANT_ID", "SEARCH_SERVICE_PRINCIPAL_ID", "AZURE_AI_SEARCH_SERVICE_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    print("Work IQ Entra inputs are valid. No resources were changed.")


def main() -> None:
    """Parse arguments and run validation or apply the Work IQ Entra configuration."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
        return
    try:
        apply()
    except GraphNotReadyError as error:
        raise SystemExit(f"ERROR: {error} {PERMISSION_HINT}") from None
    except Exception as error:  # noqa: BLE001 - report any failure as a clean exit code
        raise SystemExit(f"ERROR: {error}") from None


if __name__ == "__main__":
    main()
