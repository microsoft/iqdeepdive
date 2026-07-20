# AGENTS.md

This project was built with the `microsoft-foundry` skill. Before working on or answering questions about Foundry
agents, read the `microsoft-foundry` skill first.

## Project overview

This repository contains a five-part Microsoft Foundry IQ notebook lab and five Python agents deployed as
Microsoft Foundry hosted agents. A single Azure Developer CLI (`azd`) project provisions the shared Foundry project,
model deployments, Azure AI Search, storage, monitoring, and optional Microsoft Fabric capacity.

The notebooks and hosted agents share infrastructure but create separate knowledge bases. Three hosted agents use
`contoso-company-kb`: `agent-foundry-iq-mcp` connects through the Azure AI Search knowledge-base MCP endpoint,
`agent-foundry-iq-api` calls the knowledge-base retrieval API with a custom Python tool, and
`agent-foundry-iq-toolbox` connects through a Foundry toolbox.
`agent-foundry-iq-fabric-toolbox` uses a separate toolbox and a delegated-user connection to the Fabric IQ ontology
created by `infra/create-lakehouse.py`.
`agent-foundry-iq-workiq-toolbox` uses a separate OAuth2 `RemoteA2A` connection and toolbox to access the signed-in
user's Microsoft 365 work context through Work IQ.

## Repository map

- `azure.yaml`: `azd` service manifest. It declares the existing Foundry project service, the
  five hosted services, Python 3.13 remote build settings, runtime
  environment variables, and deployment hooks.
- `pyproject.toml` and `uv.lock`: root Python environment for infrastructure helpers and notebook support. Use this
  environment for files under `infra/`.
- `README.md`: user-facing setup, deployment, notebook, and local-agent instructions.
- `ATTRIBUTION.md`: upstream sources, revisions, and retained attribution.
- `data/`: source documents and exported JSONL/index definitions used to seed Azure AI Search.
- `data/ai-search-data/`: files uploaded or indexed for the notebook and agent examples.
- `data/index-data/`: exported Search indexes and index metadata restored during provisioning.
- `infra/main.bicep`: subscription-scope entry point. Creates the resource group and composes Foundry, Search,
  storage, monitoring, and optional Fabric modules.
- `infra/main.parameters.json`: maps `azd` environment values into the Bicep deployment.
- `infra/core/`: reusable Bicep modules for Foundry, Search, storage, monitoring, and Fabric resources.
- `infra/create-search-indexes.py`: restores sample indexes and creates the hosted agent's
  `contoso-company-kb`.
- `infra/create-toolbox.py`: creates and promotes the toolbox version after the knowledge base exists. It uses the
  dedicated `kb-mcp-connection` remote-tool project connection by default.
- `infra/create-fabric-toolbox.py`: creates the `user-entra-token` Fabric ontology connection after the ontology
  exists, then creates and promotes the separate Fabric IQ toolbox.
- `infra/create-workiq-toolbox.py`: opt-in Graph SDK setup for the Work IQ service principal, single-tenant Entra
  app, delegated consent, OAuth2 `RemoteA2A` connection, callback URI, and separate Work IQ toolbox.
- `infra/create-lakehouse.py`: creates optional Fabric lakehouse and ontology resources.
- `infra/setup-env.py`: writes generated Azure outputs to the local `.env` used by notebooks and local agent runs.
- `infra/hooks/postprovision.sh` and `infra/hooks/postprovision.ps1`: run after infrastructure provisioning to
  write local settings, seed Search, and optionally prepare Fabric.
- `infra/hooks/postdeploy.sh` and `infra/hooks/postdeploy.ps1`: run after agent deployment, resolve the generated
  hosted-agent identity, and grant it `Search Index Data Contributor` on Azure AI Search.
- `notebooks/`: the five ordered Foundry IQ lab notebooks. Their extra kernel dependencies are listed in
  `notebooks/requirements.txt`.
- `src/agent-foundry-iq-mcp/main.py`: Agent Framework application. It exposes a Responses server, uses Foundry for chat, and
  connects to the Search knowledge base with an authenticated `MCPStreamableHTTPTool`.
- `src/agent-foundry-iq-api/main.py`: sibling Agent Framework application whose custom Python tool calls the Azure AI Search
  knowledge-base retrieval API with `KnowledgeBaseRetrievalClient`.
- `src/agent-foundry-iq-toolbox/main.py`: sibling Agent Framework application that accesses the knowledge base,
  web search, and code interpreter through `FoundryToolbox`.
- `src/agent-foundry-iq-fabric-toolbox/main.py`: sibling Agent Framework application that accesses product and
  inventory data through the Fabric IQ ontology toolbox.
- `src/agent-foundry-iq-workiq-toolbox/main.py`: sibling Agent Framework application that accesses the signed-in
  user's Microsoft 365 work context through the Work IQ toolbox.
- `src/agent-foundry-iq-mcp/pyproject.toml`, `src/agent-foundry-iq-mcp/uv.lock`, and `src/agent-foundry-iq-mcp/uv.toml`: isolated MCP agent dependency
  definition, lockfile, and remote-build TLS configuration.
- `src/agent-foundry-iq-api/pyproject.toml`, `src/agent-foundry-iq-api/uv.lock`, and `src/agent-foundry-iq-api/uv.toml`: isolated API agent
  dependency definition, lockfile, and remote-build TLS configuration.
- `src/agent-foundry-iq-toolbox/pyproject.toml`, `src/agent-foundry-iq-toolbox/uv.lock`, and
  `src/agent-foundry-iq-toolbox/uv.toml`: isolated toolbox agent dependency definition, lockfile, and remote-build
  TLS configuration.
- `src/agent-foundry-iq-fabric-toolbox/pyproject.toml`, `src/agent-foundry-iq-fabric-toolbox/uv.lock`, and
  `src/agent-foundry-iq-fabric-toolbox/uv.toml`: isolated Fabric toolbox agent dependency definition, lockfile, and
  remote-build TLS configuration.
- `src/agent-foundry-iq-workiq-toolbox/pyproject.toml`, `src/agent-foundry-iq-workiq-toolbox/uv.lock`, and
  `src/agent-foundry-iq-workiq-toolbox/uv.toml`: isolated Work IQ agent dependency definition, lockfile, and
  remote-build TLS configuration.

## Development guidance

Keep changes scoped to the owning environment:

- Infrastructure and seeding dependencies belong in the root `pyproject.toml` and root `uv.lock`.
- Hosted-agent runtime dependencies belong in each agent's own `pyproject.toml` and `uv.lock`.
- Do not assume a package available in the root `.venv` exists in the hosted agent.
- Keep the agent compatible with the Python runtime configured in `azure.yaml` (`python_3_13`).
- Preserve direct source deployment unless the agent begins requiring custom OS packages.
- Use structured Azure SDK APIs for resource and data operations; use shell hooks only for lifecycle work that
  depends on an identity created by `azd deploy`.
- Maintain both POSIX and PowerShell hooks when changing deployment behavior.
- Do not commit `.env`, credentials, generated tokens, or local virtual environments.

The MCP server requires authentication during `session.initialize()`. Attach Azure bearer authentication to the
supplied `httpx.AsyncClient`; do not replace it with only `header_provider`, which applies runtime tool-call headers
too late for initialization.

## Upstream Agent Framework issues

Keep these open Python hosting issues in mind when changing the Work IQ consent flow:

- [microsoft/agent-framework#5594](https://github.com/microsoft/agent-framework/issues/5594):
  `ResponsesHostServer` cannot automatically resume an interrupted turn after OAuth consent. The user must resend
  the original request.
- [microsoft/agent-framework#7227](https://github.com/microsoft/agent-framework/issues/7227):
  the Python consent parser recognizes MCP sources but not Work IQ's `a2a_preview` source, so the unmodified host
  does not surface Work IQ `CONSENT_REQUIRED` errors as `oauth_consent_request` output.
- [microsoft/agent-framework#7166](https://github.com/microsoft/agent-framework/issues/7166):
  after consent, reconnecting the same `FoundryToolbox` instance loses its authenticated HTTP client. Resending in
  the same hosted session returns `401 Unauthorized` during MCP initialization, while a fresh agent session and
  conversation succeeds. This issue includes a reproduced root cause and Work IQ validation.

## Local validation

Install and validate root tooling:

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run python -m compileall -q infra src/agent-foundry-iq-mcp src/agent-foundry-iq-api src/agent-foundry-iq-toolbox src/agent-foundry-iq-fabric-toolbox src/agent-foundry-iq-workiq-toolbox
az bicep build --file infra/main.bicep --stdout > /dev/null
azd show
```

Validate the hosted-agent package separately:

```bash
uv sync --project src/agent-foundry-iq-mcp --python 3.13 --frozen --dry-run
uv run --project src/agent-foundry-iq-mcp --python 3.13 python -m py_compile src/agent-foundry-iq-mcp/main.py
uv sync --project src/agent-foundry-iq-api --python 3.13 --frozen --dry-run
uv run --project src/agent-foundry-iq-api --python 3.13 python -m py_compile src/agent-foundry-iq-api/main.py
uv sync --project src/agent-foundry-iq-toolbox --python 3.13 --frozen --dry-run
uv run --project src/agent-foundry-iq-toolbox --python 3.13 python -m py_compile src/agent-foundry-iq-toolbox/main.py
uv sync --project src/agent-foundry-iq-fabric-toolbox --python 3.13 --frozen --dry-run
uv run --project src/agent-foundry-iq-fabric-toolbox --python 3.13 python -m py_compile src/agent-foundry-iq-fabric-toolbox/main.py
uv sync --project src/agent-foundry-iq-workiq-toolbox --python 3.13 --frozen --dry-run
uv run --project src/agent-foundry-iq-workiq-toolbox --python 3.13 python -m py_compile src/agent-foundry-iq-workiq-toolbox/main.py
```

Validate deployment hooks after editing them:

```bash
sh -n infra/hooks/postdeploy.sh
azd hooks run postdeploy
```

Use `azd hooks run postdeploy` to retry the postdeploy role-assignment step without rerunning provisioning or agent deployment. The hook uses `AzureDeveloperCliCredential` and requires the active azd environment to provide `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, and `AZURE_AI_SEARCH_SERVICE_NAME`. Confirm those values with `azd env get-value` before running the hook.

Run any agent locally with the same service manifest:

```bash
azd ai agent run agent-foundry-iq-mcp
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-foundry-iq-api
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-foundry-iq-toolbox
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-foundry-iq-fabric-toolbox
azd ai agent invoke --local "Which product categories have the lowest stock levels right now?"

azd ai agent run agent-foundry-iq-workiq-toolbox
azd ai agent invoke --local \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

## Deployment workflow

For a new environment or infrastructure changes:

```bash
azd auth login
azd up
```

For agent-only code or dependency changes:

```bash
azd deploy agent-foundry-iq-mcp
azd ai agent invoke agent-foundry-iq-mcp "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-api
azd ai agent invoke agent-foundry-iq-api "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-toolbox
azd ai agent invoke agent-foundry-iq-toolbox "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-fabric-toolbox
azd ai agent invoke agent-foundry-iq-fabric-toolbox --new-session --new-conversation \
  "Which product categories have the lowest stock levels right now?"

azd deploy agent-foundry-iq-workiq-toolbox
azd ai agent invoke agent-foundry-iq-workiq-toolbox --new-session --new-conversation \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

`azd up` performs these phases:

1. Bicep provisions shared Azure resources.
2. `postprovision` writes `.env`, restores Search data, creates the agent knowledge base and toolbox, and optionally
  configures Fabric.
3. Foundry remotely builds and deploys all five agent packages under `src/`.
4. `postdeploy` grants Search data access to the three Search-backed agents. The Fabric toolbox agent instead uses
  the invoking user's delegated Fabric permissions.
    The Work IQ agent additionally receives Foundry User at account and project scopes and uses the caller's
    delegated Microsoft 365 identity.

Do not move the hosted-agent role assignment into Bicep or `postprovision` unless the identity lifecycle changes.
The instance identity does not exist until the agent has been deployed.

## Deployment troubleshooting

### Remote build or TLS failure

Keep each agent's `uv.toml` with `system-certs = true`. Regenerate the corresponding lockfile with Python 3.13
after dependency changes:

```bash
uv lock --project src/agent-foundry-iq-mcp --python 3.13
uv sync --project src/agent-foundry-iq-mcp --python 3.13 --frozen --dry-run

uv lock --project src/agent-foundry-iq-api --python 3.13
uv sync --project src/agent-foundry-iq-api --python 3.13 --frozen --dry-run

uv lock --project src/agent-foundry-iq-toolbox --python 3.13
uv sync --project src/agent-foundry-iq-toolbox --python 3.13 --frozen --dry-run

uv lock --project src/agent-foundry-iq-fabric-toolbox --python 3.13
uv sync --project src/agent-foundry-iq-fabric-toolbox --python 3.13 --frozen --dry-run

uv lock --project src/agent-foundry-iq-workiq-toolbox --python 3.13
uv sync --project src/agent-foundry-iq-workiq-toolbox --python 3.13 --frozen --dry-run
```

### Missing runtime setting

Every required hosted variable must be listed under the corresponding agent's `environmentVariables` in
`azure.yaml`. Local `.env` values are not automatically available in the hosted container.

### MCP cancellation error

A message such as `MCP server failed to initialize: Cancelled via cancel scope` often masks the HTTP failure that
caused the MCP transport to close. Inspect the preceding `httpx` log line:

- `401 Unauthorized`: the initialization request did not carry a valid bearer token. Check the authenticated
  `httpx.AsyncClient` in `src/agent-foundry-iq-mcp/main.py`.
- `403 Forbidden`: authentication succeeded, but the hosted agent's generated managed identity lacks Search RBAC
  or the role assignment has not propagated yet.

The `postdeploy` hook grants the required Search role. Azure RBAC may take a few minutes to propagate; retry in a
fresh invocation before changing application code.

### Inspect deployed sessions

List sessions and monitor the exact failing session rather than mixing logs from older versions:

```bash
azd ai agent sessions list --output json
azd ai agent monitor agent-foundry-iq-mcp --session-id <session-id> --type console --tail 300 --utc
azd ai agent monitor agent-foundry-iq-mcp --session-id <session-id> --type system --tail 300 --utc
```

Confirm the active deployment and its generated identity with:

```bash
azd ai agent show agent-foundry-iq-mcp --output json
```

A healthy knowledge-base request should show successful Search MCP responses followed by
`knowledge_base_retrieve succeeded`.

## Azure CLI safety

Before provisioning, deploying, assigning roles, or monitoring, verify the selected Azure subscription, tenant,
and `azd` environment. Avoid sharing mutable Azure CLI or `azd` state across concurrent sessions. Prefer an isolated
CLI profile when automating commands, and always pass the intended `azd` environment explicitly when more than one
environment exists.
