# Foundry IQ deep dive

This repository combines a five-part Microsoft Foundry IQ notebook lab with five deployable
[Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/) agents. One `azd`
project provisions the shared Foundry project, `gpt-5.4` and `text-embedding-3-large` deployments,
Azure AI Search, storage, monitoring, and an optional F2 Fabric capacity. It then prepares Search
data, creates the agent's HR knowledge base, and deploys the agent directly from Python source.

## Architecture

```mermaid
flowchart LR
  azd[azd up] --> foundry[Foundry project and models]
  azd --> search[Azure AI Search]
  azd --> fabric[Optional Fabric F2 capacity]
  search --> agentkb[contoso-company-kb]
  agentkb -->|Direct MCP endpoint| mcpagent[Hosted MCP HR agent]
  agentkb -->|Python retrieval API tool| apiagent[Hosted API HR agent]
  agentkb --> toolbox[Foundry toolbox]
  toolbox --> toolboxagent[Hosted toolbox HR agent]
  search --> notebooks[Five notebook-created KBs]
  fabric --> notebooks
  fabric --> fabrictoolbox[Fabric IQ toolbox]
  fabrictoolbox --> fabricagent[Hosted inventory agent]
  m365[Microsoft 365] --> workiq[Work IQ A2A]
  workiq --> workiqtoolbox[Work IQ toolbox]
  workiqtoolbox --> workiqagent[Hosted workplace agent]
```

The examples intentionally remain independent. The notebooks create learning-path knowledge bases.
Three hosted agents use their own `contoso-company-kb`: one connects through its direct Foundry IQ
MCP endpoint, one calls the `2026-05-01-preview` retrieval API from a custom Python tool, and one uses
a Foundry toolbox containing the knowledge base, web search, and code interpreter tools. The fourth
agent uses a separate toolbox connected directly to the Fabric IQ ontology used by
`foundryiq-fabriciq.ipynb`. It passes through the invoking user's Entra identity and does not use a
notebook-created knowledge base.
The fifth agent uses a separate OAuth2 `RemoteA2A` connection and toolbox to query the signed-in user's
Microsoft 365 work context through Work IQ.

## Prerequisites

- An Azure subscription with permission to create resources and role assignments
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
  with the `azure.ai.agents` and `azure.ai.connections` extensions available
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12+
- Quota in one region for Foundry hosted agents, `gpt-5.4`, and `text-embedding-3-large`
- For notebook parts 3 and 5, a Fabric-capable tenant and a Fabric/Power BI license (or active Fabric
  trial) assigned to the account used by `az login`; the default deployment creates an F2 capacity
- For the Fabric toolbox agent, the same Fabric license and ontology access assigned to the account
  used by `azd auth login`; its `user-entra-token` connection uses the invoking user's permissions
- For part 2, a `WEB_IQ_KEY` supplied by the Build lab organizer
- For parts 4 and 5, Microsoft-approved access to Work IQ retrieval through Azure AI Search, as
  described below
- For the Work IQ agent, a Microsoft 365 Copilot license for each caller, a tenant enabled for Work IQ,
  and a Global Administrator for the one-time Entra app registration and admin consent

## Provision and deploy

```bash
azd auth login
azd up
```

`azd up` provisions the resources, writes the generated local settings to `.env`, restores the
sample HR and health indexes, creates the independent HR agent knowledge base and Foundry toolbox,
prepares Fabric when enabled, creates a separate `fabric-ontology-tools` toolbox, and deploys all five
agents. The Fabric toolbox targets the generated ontology endpoint exactly and uses the
`fabric-ontology-connection` remote-tool connection. No Azure resources are included in this repository.

Set `DEPLOY_FABRIC_CAPACITY=false` before `azd up` to use an existing Fabric workspace or skip the
Fabric portions. Set `FABRIC_WORKSPACE_ID` and `FABRIC_ONTOLOGY_ID` in `.env` before running parts 3
and 5 when you manage Fabric separately.

### Enable Work IQ retrieval for Azure AI Search

Notebook parts 4 and 5 use a Work IQ knowledge source in Azure AI Search. This preview is disabled by
default and must be approved by Microsoft. An identity with Owner or Contributor on the subscription
must register the preview feature and then re-register the Search resource provider:

```bash
az feature register \
  --namespace Microsoft.Search \
  --name EnableFoundryIQWithWorkIQ \
  --subscription "<subscription-id>"
az provider register \
  --namespace Microsoft.Search \
  --subscription "<subscription-id>"
```

A Microsoft Entra administrator must also submit the
[Work IQ access request form](https://aka.ms/foundry-iq-work-iq-admin-consent-form). The subscription
role holder and Entra administrator can be different people. Wait for Microsoft to approve the request
before running the Work IQ notebooks. See
[Request access to Work IQ retrieval](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-work-iq#request-access-to-work-iq-retrieval)
for the current requirements.

### Enable the hosted Work IQ toolbox agent

Work IQ setup is opt-in because its first run requires a Global Administrator and creates a tenant app
registration. Authenticate azd as that administrator, enable Work IQ, and rerun provisioning:

```bash
az ad sp create --id fdcc1f02-fc51-4226-8753-f668596af7f7
azd env set ENABLE_WORK_IQ true
azd provision
```

The postprovision helper creates or reuses the Work IQ service principal, creates a single-tenant Entra
application with delegated `WorkIQAgent.Ask`, grants admin consent, creates the OAuth2 `RemoteA2A`
connection, adds Foundry's returned callback URI to the application, and publishes `work-iq-tools`.
The generated client secret is sent directly to the connection and is not written to `.env`.

Admin consent allows the application to request `WorkIQAgent.Ask`; it does not authorize individual
users. On a caller's first Work IQ request, the agent returns an `oauth_consent_request` containing a
short-lived sign-in link. That user opens the link once and then retries the request. Each caller must
complete this delegated OAuth authorization with their own Microsoft 365 identity.

## Run the notebooks

Install the notebook kernel into the root environment:

```bash
uv sync --locked --all-groups
uv pip install --python .venv/bin/python -r notebooks/requirements.txt
```

Add the externally supplied `WEB_IQ_KEY` to `.env` for part 2. Then open `notebooks/` in VS Code,
select `.venv/bin/python`, and run these in order:

1. `part1-standard-foundry-iq-kb.ipynb`
2. `part2-search-mcp-kb.ipynb`
3. `part3-fabric-iq-to-kb.ipynb`
4. `part4-work-iq-to-kb.ipynb`
5. `part5-work-iq-fabric-iq-to-kb.ipynb`

## Run and invoke the HR agents

Start either hosted-agent source locally:

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

Redeploy an individual agent after code changes and invoke the deployed version:

```bash
azd deploy agent-foundry-iq-mcp
azd ai agent invoke agent-foundry-iq-mcp "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-api
azd ai agent invoke agent-foundry-iq-api "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-toolbox
azd ai agent invoke agent-foundry-iq-toolbox "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundry-iq-fabric-toolbox
azd ai agent invoke agent-foundry-iq-fabric-toolbox \
  --new-session --new-conversation \
  "Which product categories have the lowest stock levels right now?"

azd deploy agent-foundry-iq-workiq-toolbox
azd ai agent invoke agent-foundry-iq-workiq-toolbox \
  --new-session --new-conversation \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

Direct source deployment is used because the final agent requires no custom OS packages. Foundry's
remote build resolves each agent folder's `pyproject.toml` and `uv.lock`, avoiding an unnecessary
container registry and image-build path.

## Validate locally

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run python -m compileall -q infra src/agent-foundry-iq-mcp src/agent-foundry-iq-api src/agent-foundry-iq-toolbox src/agent-foundry-iq-fabric-toolbox src/agent-foundry-iq-workiq-toolbox
uv run python scripts/check_repo.py
az bicep build --file infra/main.bicep --stdout > /dev/null
azd show
```

## Resources

- [Mastering Foundry Toolbox](https://github.com/microsoft-foundry/forgebook/blob/main/notebooks/mastering-foundry-toolbox.ipynb)

See [ATTRIBUTION.md](ATTRIBUTION.md) for the exact upstream revisions and retained licenses.
