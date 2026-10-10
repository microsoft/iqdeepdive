"""Shared helpers for the Work IQ notebooks.

Handles delegated auth against the Work IQ Gateway. Every call runs as the signed-in user and
honors their Microsoft 365 permissions and sensitivity labels.
"""

import os

from dotenv import load_dotenv

load_dotenv()

WORK_IQ_GATEWAY = os.environ.get("WORK_IQ_GATEWAY", "https://workiq.svc.cloud.microsoft")
WORK_IQ_SCOPE = os.environ.get(
    "WORK_IQ_SCOPE", "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"
)
ENTRA_APP_ID = os.environ.get("ENTRA_APP_ID", "")
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "common")


def get_user_token() -> str:
    """Acquire a delegated Work IQ token interactively (MSAL public client).

    Requires an Entra app registration with WorkIQAgent.Ask admin-consented (see docs/workiq-admin-setup.md)
    and a Microsoft 365 Copilot license on the signed-in user.
    """
    from msal import PublicClientApplication, SerializableTokenCache

    if not ENTRA_APP_ID:
        raise RuntimeError("Set ENTRA_APP_ID in .env — see docs/workiq-admin-setup.md")

    import tempfile

    cache_path = os.path.join(tempfile.gettempdir(), "workiq_msal_cache.bin")
    cache = SerializableTokenCache()
    if os.path.exists(cache_path):
        cache.deserialize(open(cache_path).read())

    app = PublicClientApplication(
        client_id=ENTRA_APP_ID,
        authority=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}",
        token_cache=cache,
    )
    scopes = [WORK_IQ_SCOPE]

    accounts = app.get_accounts()
    result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if not result:
        if os.environ.get("CODESPACES") == "true":
            # A browser pop-up is not available in GitHub Codespaces, so sign in with a device code.
            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise RuntimeError(f"Device flow failed: {flow.get('error_description', flow)}")
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)
        else:
            result = app.acquire_token_interactive(scopes)

    if cache.has_state_changed:
        open(cache_path, "w").write(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    return result["access_token"]


def call_mcp(token: str, method: str, params: dict, rid: int = 1) -> dict:
    """Call the Work IQ MCP endpoint (JSON-RPC over Streamable HTTP / SSE).

    Work IQ speaks MCP at ``{gateway}/mcp``. Requests are JSON-RPC 2.0 and responses come back as
    Server-Sent Events (``data:`` lines), so we parse the first data frame. There is no separate
    REST "ask" route — the natural-language entry point is the ``ask`` tool over MCP.
    """
    import json

    import requests

    resp = requests.post(
        f"{WORK_IQ_GATEWAY}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        timeout=180,
    )
    resp.raise_for_status()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {"raw": resp.text}


def ask(token: str, question: str, **kwargs) -> dict:
    """Convenience wrapper for the Work IQ ``ask`` tool over MCP."""
    args = {"question": question, **kwargs}
    return call_mcp(token, "tools/call", {"name": "ask", "arguments": args}, rid=1)


def call_tool(token: str, name: str, arguments: dict | None = None) -> dict:
    """Call any Work IQ MCP tool by name and return the parsed JSON-RPC response.

    Tools: ask, list_agents, search_paths, get_schema, fetch, call_function,
    create_entity, update_entity, delete_entity, do_action.
    """
    return call_mcp(token, "tools/call", {"name": name, "arguments": arguments or {}})

