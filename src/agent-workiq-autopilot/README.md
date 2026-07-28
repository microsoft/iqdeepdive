# Workmate — Work IQ Agent 365 digital worker (hosted, activity protocol)

This folder is a **Foundry Hosted** Agent 365 digital worker that answers in
Microsoft Teams and is grounded in the signed-in user's workplace context through the
**Work IQ MCP** endpoint.

It is the live finale of the Work IQ Deep Dive: notebooks → MAF agent → a Teams autopilot
that reads and acts on your work, all through Work IQ.

## Runtime flow

```
Teams  →  Azure Bot (workmate-agent-bot)
       →  Foundry activity endpoint (.../agents/workmate-agent/endpoint/protocols/activityprotocol)
       →  hosted container  (host_agent_server.py, Bot Framework activity_protocol v1)
       →  FoundryDigitalWorkerAgent.process_user_message  (agent.py)
       →  Azure OpenAI Responses API  +  MCP tool { server_url: Work IQ /mcp }
```

The MCP HTTP call is made **server-side by the Azure OpenAI Responses API**, not by the
container — the container only hands AOAI the `server_url` + an exchanged bearer token.

## Where Work IQ is wired

- **`ToolingManifest.json`** — the single MCP server the agent exposes:
  `https://workiq.svc.cloud.microsoft/mcp`, audience `api://workiq.svc.cloud.microsoft`,
  scope `WorkIQAgent.Ask`.
- **`agent.py`**
  - `_load_mcp_servers()` reads the manifest.
  - `_build_mcp_tools()` emits one `type: mcp` tool per manifest entry, with
    `server_url` taken **directly from the manifest** and a per-server bearer token.
  - `_exchange_scope_for_server()` mints `{audience}/.default` so each server gets a
    token for its own resource (Work IQ, or the legacy A365 tool servers).
  - `AGENT_PROMPT` instructs the model to ground answers via Work IQ (`ask`, `fetch`,
    `do_action`, ...).

## Consent (one-time, per digital-worker identity)

The container requests tokens as its **ServiceIdentity** service principal
(displayName = the agent name, e.g. `Work Mate`; `objId == appId`) — **not** the
AgentIdentityBlueprint SP. Grant the Work IQ scope to that ServiceIdentity or MCP tools
fail with `AADSTS65001 consent_required`:

```powershell
# clientId  = ServiceIdentity SP objId (== appId) of the digital worker
# resourceId = Work IQ SP objId (az ad sp show --id api://workiq.svc.cloud.microsoft --query id)
# scope      = WorkIQAgent.Ask
POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants
{ "clientId": "<serviceIdentity objId>", "consentType": "AllPrincipals",
  "resourceId": "<workiq SP objId>", "scope": "WorkIQAgent.Ask" }
```

## Build & deploy

```powershell
# 1. Build the container in ACR (project system-MI needs AcrPull on the registry)
cd src/agent-workiq-autopilot
az acr build --registry <acr> --image workmate-a365-agent:latest --file ./foundry-infra/Dockerfile `
  --build-arg BLUEPRINT_CLIENT_ID=<blueprintClientId> `
  --build-arg INSTANCE_CLIENT_ID=<instanceClientId> `
  --build-arg AUTHORITY_ENDPOINT=https://login.microsoftonline.com/<tenantId> `
  --build-arg TENANT_ID=<tenantId> `
  --build-arg AZURE_OPENAI_ENDPOINT=https://<account>.openai.azure.com/ `
  --build-arg MODEL_DEPLOYMENT=<model> .

# 2. Create a new hosted agent version (kind: hosted, activity_protocol v1) pinned to the
#    new image digest, via the Foundry versions API
#    POST {project}/agents/{name}/versions?api-version=2025-11-15-preview

# 3. The Azure Bot messaging endpoint must be (lowercase, 2025-11-15-preview):
#    https://<account>.services.ai.azure.com/api/projects/<project>/agents/<name>/endpoint/protocols/activityprotocol?api-version=2025-11-15-preview
```

Traffic routes to `@latest` at 100%, so a new version rolls out automatically once its
container is healthy (allow a few minutes; until then the previous version keeps serving).

> Source adapted from the official sample
> `microsoft-foundry/foundry-samples/samples/python/foundry-autopilot-agent`
> (flattened from the `hello_world_a365_agent` package into this folder's top-level
> modules, manifest/prompt retargeted to Work IQ). See `../../ATTRIBUTION.md`.
