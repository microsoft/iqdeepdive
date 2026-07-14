# AGENTS.md

This project was built with the `microsoft-foundry` skill. Before working on or answering questions about Foundry
agents, read the `microsoft-foundry` skill first.

## Project overview

This repository contains a five-part Microsoft Foundry IQ notebook lab and two Python HR agents deployed as
Microsoft Foundry hosted agents. A single Azure Developer CLI (`azd`) project provisions the shared Foundry project,
model deployments, Azure AI Search, storage, monitoring, and optional Microsoft Fabric capacity.

The notebooks and hosted agent share infrastructure but create separate knowledge bases. The hosted agent always
uses `contoso-company-kb` through the Azure AI Search knowledge-base MCP endpoint.

## Repository map

- `azure.yaml`: `azd` service manifest. It declares the existing Foundry project service, the `hr-agent` hosted
  service, Python 3.13 remote build settings, runtime environment variables, and deployment hooks.
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
- `infra/create-lakehouse.py`: creates optional Fabric lakehouse and ontology resources.
- `infra/setup-env.py`: writes generated Azure outputs to the local `.env` used by notebooks and local agent runs.
- `infra/hooks/postprovision.sh` and `infra/hooks/postprovision.ps1`: run after infrastructure provisioning to
  write local settings, seed Search, and optionally prepare Fabric.
- `infra/hooks/postdeploy.sh` and `infra/hooks/postdeploy.ps1`: run after agent deployment, resolve the generated
  hosted-agent identity, and grant it `Search Index Data Contributor` on Azure AI Search.
- `notebooks/`: the five ordered Foundry IQ lab notebooks. Their extra kernel dependencies are listed in
  `notebooks/requirements.txt`.
- `src/hr-agent/main.py`: Agent Framework application. It exposes a Responses server, uses Foundry for chat, and
  connects to the Search knowledge base with an authenticated `MCPStreamableHTTPTool`.
- `src/hr-agent-api/main.py`: sibling Agent Framework application whose custom Python tool calls the Azure AI Search
  knowledge-base retrieval API with `KnowledgeBaseRetrievalClient`.
- `src/hr-agent/pyproject.toml` and `src/hr-agent/uv.lock`: isolated hosted-agent dependency definition and
  lockfile. Foundry remote build resolves this package independently of the root environment.
- `src/hr-agent/uv.toml`: enables system TLS certificates for Foundry's remote `uv` build.

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

## Local validation

Install and validate root tooling:

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run python -m compileall -q infra src/hr-agent
az bicep build --file infra/main.bicep --stdout > /dev/null
azd show
```

Validate the hosted-agent package separately:

```bash
uv sync --project src/hr-agent --python 3.13 --frozen --dry-run
uv run --project src/hr-agent --python 3.13 python -m py_compile src/hr-agent/main.py
uv sync --project src/hr-agent-api --python 3.13 --frozen --dry-run
uv run --project src/hr-agent-api --python 3.13 python -m py_compile src/hr-agent-api/main.py
```

Validate deployment hooks after editing them:

```bash
sh -n infra/hooks/postdeploy.sh
azd hooks run postdeploy
```

Run the agent locally with the same service manifest:

```bash
azd ai agent run
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"
```

## Deployment workflow

For a new environment or infrastructure changes:

```bash
azd auth login
azd up
```

For agent-only code or dependency changes:

```bash
azd deploy hr-agent
azd ai agent invoke hr-agent "What benefits are available, and when do I need to enroll?"
```

`azd up` performs these phases:

1. Bicep provisions shared Azure resources.
2. `postprovision` writes `.env`, restores Search data, creates the agent knowledge base, and optionally configures
  Fabric.
3. Foundry remotely builds and deploys `src/hr-agent`.
4. `postdeploy` obtains `instance_identity.principal_id` from `azd ai agent show hr-agent` and grants Search data
  access.

Do not move the hosted-agent role assignment into Bicep or `postprovision` unless the identity lifecycle changes.
The instance identity does not exist until the agent has been deployed.

## Deployment troubleshooting

### Remote build or TLS failure

Keep `src/hr-agent/uv.toml` with `system-certs = true`. Regenerate the agent lockfile with Python 3.13 after
dependency changes:

```bash
uv lock --project src/hr-agent --python 3.13
uv sync --project src/hr-agent --python 3.13 --frozen --dry-run
```

### Missing runtime setting

Every required hosted variable must be listed under `hr-agent.environmentVariables` in `azure.yaml`. Local `.env`
values are not automatically available in the hosted container.

### MCP cancellation error

A message such as `MCP server failed to initialize: Cancelled via cancel scope` often masks the HTTP failure that
caused the MCP transport to close. Inspect the preceding `httpx` log line:

- `401 Unauthorized`: the initialization request did not carry a valid bearer token. Check the authenticated
  `httpx.AsyncClient` in `src/hr-agent/main.py`.
- `403 Forbidden`: authentication succeeded, but the hosted agent's generated managed identity lacks Search RBAC
  or the role assignment has not propagated yet.

The `postdeploy` hook grants the required Search role. Azure RBAC may take a few minutes to propagate; retry in a
fresh invocation before changing application code.

### Inspect deployed sessions

List sessions and monitor the exact failing session rather than mixing logs from older versions:

```bash
azd ai agent sessions list --output json
azd ai agent monitor hr-agent --session-id <session-id> --type console --tail 300 --utc
azd ai agent monitor hr-agent --session-id <session-id> --type system --tail 300 --utc
```

Confirm the active deployment and its generated identity with:

```bash
azd ai agent show hr-agent --output json
```

A healthy knowledge-base request should show successful Search MCP responses followed by
`knowledge_base_retrieve succeeded`.

## Azure CLI safety

Before provisioning, deploying, assigning roles, or monitoring, verify the selected Azure subscription, tenant,
and `azd` environment. Avoid sharing mutable Azure CLI or `azd` state across concurrent sessions. Prefer an isolated
CLI profile when automating commands, and always pass the intended `azd` environment explicitly when more than one
environment exists.
