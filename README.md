<!--
---
name: Microsoft IQ Deep Dive with Python
description: Collection of Python examples for Microsoft Agent Framework using Microsoft Foundry.
languages:
- python
products:
- azure-openai
- azure
- ai-services
page_type: sample
urlFragment: iqdeepdive
---
-->

# Microsoft IQ Deep Dive with Python

This is the code companion for the [Microsoft IQ Deep Dive with Python](https://aka.ms/IQDeepDivePython/series) video series.

This repository includes multiple Jupyter notebooks and [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/) agents that use Foundry IQ, Web IQ, Work IQ, Fabric IQ, or a combination.

All of the infrastructure for deployment is included in the repository, using the [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/). The infrastructure-as-code (Bicep files) will create `gpt-5.4` and `text-embedding-3-large` deployments, Azure AI Search, storage, monitoring, and an optional Fabric capacity. A sequence of scripts prepares Search data, creates Search knowledge bases, and seeds the Fabric lakehouse.

* [Azure account requirements](#azure-account-requirements)
* [Getting started](#getting-started)
  * [GitHub Codespaces](#github-codespaces)
  * [VS Code Dev Containers](#vs-code-dev-containers)
  * [Local environment](#local-environment)
* [Deploying to Azure](#deploying-to-azure)
  * [Enable Fabric IQ](#enable-fabric-iq)
  * [Enable Work IQ retrieval for Azure AI Search](#enable-work-iq-retrieval-for-azure-ai-search)
  * [Enable the hosted Foundry IQ Work IQ agent](#enable-the-hosted-foundry-iq-work-iq-agent)
  * [Enable the hosted Work IQ toolbox agent](#enable-the-hosted-work-iq-toolbox-agent)
  * [Seed sample mailbox data](#seed-sample-mailbox-data)
* [Run the notebooks](#run-the-notebooks)
* [Run and invoke the agents](#run-and-invoke-the-agents)
* [Resources](#resources)

## Azure account requirements

In order to run all of the examples, you will need a tenant with multiple licenses and permissions. However, even if you do not have all of these permissions, you may be able to run some of the examples.

* An Azure subscription with permission to create resources and role assignments
* Quota in one region for `gpt-5.4`, and `text-embedding-3-large`
* For Fabric IQ notebooks: A Fabric-capable tenant and a Fabric/Power BI license (or active Fabric trial), with permission to create an F2 capacity
* For Web IQ notebook: Access to the private preview with an assigned `WEB_IQ_KEY`
* For Work IQ notebooks and agents: A Microsoft 365 Copilot license for each caller, a tenant enabled for Work IQ,
  and a Global Administrator for the one-time Entra app registration and admin consent

## Getting started

You have a few options for getting started with this repository.
The quickest way to get started is GitHub Codespaces, since it will setup everything for you, but you can also [set it up locally](#local-environment).

### GitHub Codespaces

You can run this repository virtually by using GitHub Codespaces. The button will open a web-based VS Code instance in your browser:

1. Open the repository (this may take several minutes):

    [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Azure-Samples/python-agentframework-demos)

2. Open a terminal window
3. Continue with the steps to run the examples

### VS Code Dev Containers

A related option is VS Code Dev Containers, which will open the project in your local VS Code using the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers):

1. Start Docker Desktop (install it if not already installed)
2. Open the project:

    [![Open in Dev Containers](https://img.shields.io/static/v1?style=for-the-badge&label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/Azure-Samples/python-agentframework-demos)

3. In the VS Code window that opens, once the project files show up (this may take several minutes), open a terminal window.
4. Continue with the steps to run the examples

The dev container includes a Redis server, which is used by the `agent_history_redis.py` example.

### Local environment

1. Make sure the following tools are installed:

    * [Python 3.12+](https://www.python.org/downloads/)
    * [uv](https://docs.astral.sh/uv/getting-started/installation/)
    * [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
      with the `azure.ai.agents` and `azure.ai.connections` extensions available
    * Git

2. Clone the repository:

    ```shell
    git clone https://github.com/microsoft/iqdeepdive/
    cd iqdeepdive
    ```

3. Install the dependencies:

    ```shell
    uv sync
    ```

## Deploying to Azure

1. Login to Azure:

    ```shell
    azd auth login
    ```

    For GitHub Codespaces users, if the previous command fails, try:

   ```shell
    azd auth login --use-device-code
    ```

2. Create an `azd` environment:

    ```shell
    azd env new
    ```

    It will prompt you for an environment name (like "iqdeepdive"), a subscription from your Azure
    account, and a location.

3. Every optional service is off by default. Enable the ones you have access to now, since they are
   read during provisioning:

    ```shell
    # Fabric IQ: F2 capacity, plus the lakehouse, ontology, graph, semantic model, and Data Agent
    azd env set ENABLE_FABRIC_CAPACITY true
    azd env set ENABLE_FABRIC_ITEMS true

    # Work IQ knowledge base and toolbox, used by agent-toolbox-foundryiq-workiq
    azd env set ENABLE_WORK_IQ_KB_TOOLBOX true

    # Work IQ Entra app and toolbox, used by agent-toolbox-workiq
    azd env set ENABLE_WORK_IQ true
    ```

    Each flag has prerequisites described in its own section: [Fabric IQ](#enable-fabric-iq),
    [Work IQ retrieval for Azure AI Search](#enable-work-iq-retrieval-for-azure-ai-search),
    [the hosted Foundry IQ Work IQ agent](#enable-the-hosted-foundry-iq-work-iq-agent), and
    [the hosted Work IQ toolbox agent](#enable-the-hosted-work-iq-toolbox-agent).
    You can also set any of these later and re-run `azd provision`.

4. Provision the Azure resources:

    ```shell
    azd provision
    ```

5. Once the resources are provisioned, you should now see a local `.env` file with all the environment variables needed to run the scripts.
6. To delete the resources, run:

    ```shell
    azd down
    ```

### Enable Fabric IQ

The Fabric IQ notebooks need a Fabric workspace with a lakehouse,
ontology, product review graph, web analytics semantic model, and a published Fabric Data Agent. Fabric is
opt-in and controlled by two independent flags, one per layer:

* `ENABLE_FABRIC_CAPACITY` (default `false`) creates an F2 Fabric capacity in Bicep. Leave it `false` to
  use a workspace you already manage.
* `ENABLE_FABRIC_ITEMS` (default `false`) runs the postprovision steps that create the Fabric items: the
  lakehouse, ontology, product review graph, web analytics lakehouse, semantic model, Power BI report,
  Fabric Data Agent, and Fabric IQ toolbox. It requires either `FABRIC_CAPACITY_ID` or
  `FABRIC_WORKSPACE_ID` to be set, and fails early with a clear message when neither is present.

To create a capacity and all of the Fabric items:

```bash
azd env set ENABLE_FABRIC_CAPACITY true
azd env set ENABLE_FABRIC_ITEMS true
azd provision
```

To use a workspace you already manage, set `FABRIC_WORKSPACE_ID` in `.env` and create only the items:

```bash
azd env set ENABLE_FABRIC_ITEMS true
azd provision
```

To skip Fabric entirely, leave both flags unset. Set `FABRIC_ONTOLOGY_ID` and `FABRIC_GRAPH_ID` in `.env`
before running the Fabric IQ notebooks when you manage those items separately.

The Fabric Data Agent is published over three complementary sources. The ontology owns product, category,
supplier, store, and inventory facts; the product review graph owns reviewers, reviews, features, and
feature-level sentiment; the Direct Lake semantic model owns website traffic and conversion measures. Product
SKU is the shared key between the ontology and the graph. Provisioning selects all seven semantic model tables
before publishing the Data Agent.

Provisioning writes the generated identifiers and portal links to `.env`:

| Variables | Fabric item |
| --- | --- |
| `FABRIC_ONTOLOGY_ID`, `FABRIC_ONTOLOGY_MCP_URL`, `FABRIC_ONTOLOGY_UI_URL` | Lakehouse ontology |
| `FABRIC_GRAPH_ID`, `FABRIC_GRAPH_UI_URL` | Product review Graph Model |
| `FABRIC_DATA_AGENT_ID`, `FABRIC_DATA_AGENT_MCP_URL` | Published Fabric Data Agent |
| `FABRIC_WEB_ANALYTICS_LAKEHOUSE_*` | Web analytics lakehouse |
| `FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_*` | Direct Lake semantic model |
| `FABRIC_WEB_ANALYTICS_REPORT_ID`, `FABRIC_WEB_ANALYTICS_REPORT_URL` | Power BI report |

### Enable Work IQ retrieval for Azure AI Search

The `foundryiq-workiq.ipynb` notebook and the Work IQ knowledge base use a Work IQ knowledge source in
Azure AI Search. This preview is disabled by
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

### Enable the hosted Foundry IQ Work IQ agent

Enable the Work IQ knowledge-base and Toolbox creation hook, then rerun provisioning:

```bash
azd env set ENABLE_WORK_IQ_KB_TOOLBOX true
azd provision
azd deploy agent-toolbox-foundryiq-workiq
azd ai agent invoke agent-toolbox-foundryiq-workiq \
  --new-session --new-conversation \
  "Search my recent emails for Professional Claw Hammer and summarize requested actions. Use the knowledge base and its Work IQ source."
```

Postprovision creates `workiq-knowledge-source`, combines it with the shared HR and health index
sources in `multisource-workiq-knowledge-base`, and publishes `workiq-knowledge-tools`. The notebook
`foundryiq-workiq.ipynb` remains an independent walkthrough of the same Search configuration and is
not required to deploy the agent.

The dedicated `workiq-kb-mcp-connection` must target the same knowledge-base MCP URL used by the
Toolbox tool. Do not reuse `kb-mcp-connection`, whose target is `contoso-company-kb-minimal`. Toolbox uses the
matching Foundry IQ connection to authenticate Search with the project managed identity and emit the
signed-in user's Search-scoped token as `x-ms-query-source-authorization` for Work IQ retrieval.
Invoke this agent through its deployed endpoint: local runs do not receive the hosted platform's
`x-agent-foundry-call-id`, so Toolbox cannot resolve the signed-in caller for Work IQ retrieval.

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

### Seed sample mailbox data

To test Work IQ with predictable content, sign in to Outlook as the test user and send the following
three fixture messages to that same user's email address:

* [Urgent: Professional Claw Hammer out of stock at Seattle store](data/workiq-fixtures/urgent-professional-claw-hammer-out-of-stock.txt)
* [RE: Weekly inventory report - Seattle flagged](data/workiq-fixtures/weekly-inventory-report-seattle-flagged.txt)
* [Customer escalation - hammer unavailable again](data/workiq-fixtures/customer-escalation-hammer-unavailable.txt)

Use the subject and body from each fixture. Because Outlook cannot impersonate the named colleague,
prepend `Forwarded message from <name>, <role>` to the body rather than attempting to change the
sender. Verify that all three messages appear in the test user's Inbox, then try:

```text
Check my recent emails for messages about the Professional Claw Hammer. Summarize what colleagues
are saying and what actions have been requested.
```

Automated seeding through Microsoft Graph requires a dedicated Entra application with the
admin-consented `Mail.Send` application permission. That broad permission is not granted by this
project, so manual seeding is the recommended setup for a self-deployed test environment.

## Run the notebooks

Install the notebook kernel into the root environment:

```bash
uv sync --locked --all-groups
uv pip install --python .venv/bin/python -r notebooks/requirements.txt
```

Then open `notebooks/` in VS Code and select a notebook. Each one is independent, so you can run them in
any order, as long as you have the resources necessary.

Knowledge base notebooks, which build a Foundry IQ knowledge base over different kinds of knowledge source:

| Notebook | What it covers |
| --- | --- |
| `foundryiq-basic.ipynb` | Indexed knowledge sources over the sample HR and health documents |
| `foundryiq-webiq.ipynb` | Adds an MCP server knowledge source backed by Web IQ for web grounding. Requires `WEB_IQ_KEY` in `.env`. |
| `foundryiq-workiq.ipynb` | Adds a Work IQ knowledge source over the signed-in user's Microsoft 365 context |
| `foundryiq-fabriciq-ontology.ipynb` | Adds a Fabric ontology knowledge source, queried with a delegated user token |
| `foundryiq-fabriciq-dataagent.ipynb` | Adds a Fabric Data Agent knowledge source for analytical questions |

Endpoint notebooks, which skip the knowledge base and call a service endpoint directly:

| Notebook | What it covers |
| --- | --- |
| `foundryiq-mcp.ipynb` | Calls `knowledge_base_retrieve` on the `contoso-company-kb-minimal` MCP endpoint |
| `fabriciq-ontology-mcp.ipynb` | Inspects and calls the tools on the Fabric ontology MCP endpoint |
| `fabriciq-dataagent-mcp.ipynb` | Inspects and calls the tools on the published Fabric Data Agent MCP endpoint |
| `fabriciq-graph.ipynb` | Sends GQL to the Fabric Graph `executeQuery` REST API |
| `workiq-api-concepts.ipynb` | Work IQ API concepts and the signed-in user's work context |
| `workiq-a2a.ipynb` | Delegates a task to the Work IQ Relay Agent over A2A |
| `workiq-mcp.ipynb` | Consumes Work IQ as a tool through its MCP server |
| `workiq-tools-actions.ipynb` | The full Work IQ MCP tool catalog and its resource-path model |

### Configure local MCP servers

The repository includes a `.mcp.json` file that registers the Fabric IQ ontology MCP server for local tool discovery. The checked-in URL uses placeholder values for the workspace and ontology IDs. After provisioning, replace the placeholder URL with the value written to your local `.env` file.

On macOS or Linux, use the values from `.env` to replace the placeholders in `.mcp.json` in place:

```bash
WORKSPACE_ID=$(grep '^FABRIC_WORKSPACE_ID=' .env | sed "s/^FABRIC_WORKSPACE_ID='\(.*\)'/\1/")
ONTOLOGY_ID=$(grep '^FABRIC_ONTOLOGY_ID=' .env | sed "s/^FABRIC_ONTOLOGY_ID='\(.*\)'/\1/")
sed -i '' "s/{FABRIC_WORKSPACE_ID}/${WORKSPACE_ID}/g" .mcp.json
sed -i '' "s/{FABRIC_ONTOLOGY_ID}/${ONTOLOGY_ID}/g" .mcp.json
```

On Windows, use PowerShell to read `.env` and rewrite `.mcp.json`:

```powershell
$workspaceId = (Get-Content .env | Where-Object { $_ -match "^FABRIC_WORKSPACE_ID='(.+)'" } | ForEach-Object { $matches[1] })
$ontologyId = (Get-Content .env | Where-Object { $_ -match "^FABRIC_ONTOLOGY_ID='(.+)'" } | ForEach-Object { $matches[1] })
$mcpJson = Get-Content .mcp.json | ConvertFrom-Json
$mcpJson.mcpServers."fabric-iq-ontology".url = "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/$workspaceId/items/$ontologyId/ontologyEndpoint"
$mcpJson | ConvertTo-Json -Depth 4 | Set-Content .mcp.json
```

When you run a notebook, select the `.venv/bin/python` virtual environment.

## Run and invoke the agents

Provisioning creates two knowledge bases over the same HR and health sources: `contoso-company-kb-low`
uses low reasoning effort and the configured Azure OpenAI model for query planning, while
`contoso-company-kb-minimal` uses minimal reasoning effort and is extractive, so it configures no model.
The notebooks create their own knowledge bases and are unaffected by these.

| Agent | How it reaches its data |
| --- | --- |
| `agent-foundryiq-api` | Calls the retrieval API on `contoso-company-kb-minimal` from a custom Python tool |
| `agent-foundryiq-mcp` | Connects to the same knowledge base through its Foundry IQ MCP endpoint |
| `agent-toolbox-foundryiq` | Uses a Foundry toolbox holding that knowledge base, web search, and code interpreter |
| `agent-toolbox-foundryiq-workiq` | Uses a Foundry toolbox over the multi-source Work IQ knowledge base |
| `agent-toolbox-workiq` | Uses an OAuth2 `RemoteA2A` connection and toolbox for the caller's Microsoft 365 context |
| `agent-workiq-autopilot` | Hosted Agent 365 autopilot in Teams; grounds and acts through the Work IQ MCP server |

Start any of the agents locally, and test them with either the local playground or CLI:

```bash
azd ai agent run agent-foundryiq-api
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-foundryiq-mcp
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-toolbox-foundryiq
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-toolbox-workiq
azd ai agent invoke --local \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

You can also invoke the deployed versions. You may want to re-deploy before invoking, if you've made any changes:

```bash
azd deploy agent-foundryiq-api
azd ai agent invoke agent-foundryiq-api "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundryiq-mcp
azd ai agent invoke agent-foundryiq-mcp "What benefits are available, and when do I need to enroll?"

azd deploy agent-toolbox-foundryiq
azd ai agent invoke agent-toolbox-foundryiq "What benefits are available, and when do I need to enroll?"

azd deploy agent-toolbox-foundryiq-workiq
azd ai agent invoke agent-toolbox-foundryiq-workiq \
  --new-session --new-conversation \
  "Search my recent emails for Professional Claw Hammer and summarize requested actions. Use the knowledge base and its Work IQ source."

azd deploy agent-toolbox-workiq
azd ai agent invoke agent-toolbox-workiq \
  --new-session --new-conversation \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

Direct source deployment is used because the final agent requires no custom OS packages. Foundry's
remote build resolves each agent folder's `pyproject.toml` and `uv.lock`, avoiding an unnecessary
container registry and image-build path.

## Resources

The IQ workloads:

* [What is Foundry IQ?](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/what-is-foundry-iq)
* [What is Fabric IQ?](https://learn.microsoft.com/fabric/iq/overview)
* [Work IQ MCP tool reference](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference)

Foundry IQ knowledge bases and agentic retrieval:

* [Agentic retrieval overview](https://learn.microsoft.com/azure/search/agentic-retrieval-overview)
* [What is a knowledge source?](https://learn.microsoft.com/azure/search/agentic-knowledge-source-overview)
* [Set the retrieval reasoning effort](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-set-retrieval-reasoning-effort), which explains the difference between the `low` and `minimal` knowledge bases created here
* [Query a knowledge base via API or MCP](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve)
* [Create a Work IQ knowledge source](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-work-iq)
* [Create a Fabric Ontology knowledge source](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-fabric-ontology)

Fabric items used by the Fabric IQ notebooks:

* [Fabric data agent concepts](https://learn.microsoft.com/fabric/data-science/concept-data-agent)
* [Direct Lake overview](https://learn.microsoft.com/fabric/fundamentals/direct-lake-overview)

Building and deploying the agents:

* [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/)
* [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/)
* [Mastering Foundry Toolbox](https://github.com/microsoft-foundry/forgebook/blob/main/notebooks/mastering-foundry-toolbox.ipynb)
* [Model Context Protocol](https://modelcontextprotocol.io/)