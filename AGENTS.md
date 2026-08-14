# AGENTS.md

This project was built with the `microsoft-foundry` skill. Before working on or answering questions about Foundry
agents, read the `microsoft-foundry` skill first.

## Project overview

This repository contains a six-part Microsoft Foundry IQ notebook lab and six Python agents deployed as
Microsoft Foundry hosted agents. A single Azure Developer CLI (`azd`) project provisions the shared Foundry project,
model deployments, Azure AI Search, storage, monitoring, and optional Microsoft Fabric capacity.

The notebooks and hosted agents share infrastructure but create separate knowledge bases. The hosted-agent setup
creates `contoso-company-kb-low` with low reasoning effort and `contoso-company-kb-minimal` with minimal reasoning
effort over the same Search sources. The low KB configures the Azure OpenAI model; the extractive minimal KB does
not configure a model. Several hosted agents use `contoso-company-kb-minimal`:
`agent-foundryiq-mcp` connects through the Azure AI Search knowledge-base MCP endpoint,
`agent-foundryiq-api` calls the knowledge-base retrieval API with a custom Python tool, and
`agent-toolbox-foundryiq` connects through a Foundry toolbox.
`agent-toolbox-workiq` uses a separate OAuth2 `RemoteA2A` connection and toolbox to access the signed-in
user's Microsoft 365 work context through Work IQ.
`agent-toolbox-foundryiq-workiq` instead connects through a Foundry toolbox to the multi-source
knowledge base created by `infra/create-search-indexes.py --include-workiq`. Its dedicated PMI connection must target that exact
knowledge-base MCP URL so Toolbox emits the user's Search-scoped query-source authorization.
Provisioning also creates a separate web analytics lakehouse, Direct Lake Power BI semantic model, and PBIR
report. The semantic model feeds the same Fabric Data Agent as the ontology and Graph; provisioning selects all
seven model tables before publishing the agent.

## Sample data design

The product review graph is built from deterministic synthetic data loaded into the ontology lakehouse. Its
sentiment, confidence, and evidence values are fixture ground truth generated during provisioning; the sample
does not claim to extract sentiment with AI at runtime. Saving and refreshing the Graph Model ingests those
tables into Fabric's queryable graph representation.

The separate web analytics lakehouse contains a date dimension, channel, device, geography, and page dimensions,
plus session- and page-view-grain facts. The Direct Lake semantic model defines explicit measures such as total
sessions, unique visitors, pages per session, bounce rate, conversion rate, revenue, and average session
duration. The generated `Contoso Web Analytics Dashboard` item is technically a Power BI report: unlike a
Power BI Service dashboard, its pages and interactive visuals can be fully represented and deployed through the
Fabric API. Provisioning creates or updates the TMDL and PBIR definitions by display name.

## Repository map

- `azure.yaml`: `azd` service manifest. It declares the existing Foundry project service, the
  six hosted services, Python 3.13 remote build settings, runtime
  environment variables, and deployment hooks.
- `pyproject.toml` and `uv.lock`: root Python environment for infrastructure helpers and notebook support. Use this
  environment for files under `infra/`.
- `README.md`: user-facing setup, deployment, notebook, and local-agent instructions.
- `ATTRIBUTION.md`: upstream sources, revisions, and retained attribution.
- `data/`: source documents and exported JSONL/index definitions used to seed Azure AI Search.
- `data/ai-search-data/`: files uploaded or indexed for the notebook and agent examples.
- `data/index-data/`: exported Search indexes and index metadata restored during provisioning.
- `data/semantic-models/web-analytics/`: source-controlled TMDL definition for the Direct Lake web analytics
  semantic model.
- `data/reports/web-analytics/`: source-controlled enhanced PBIR definition for the web analytics Power BI report.
- `infra/main.bicep`: subscription-scope entry point. Creates the resource group and composes Foundry, Search,
  storage, monitoring, and optional Fabric modules.
- `infra/main.parameters.json`: maps `azd` environment values into the Bicep deployment.
- `infra/core/`: reusable Bicep modules for Foundry, Search, storage, monitoring, and Fabric resources.
- `infra/create-search-indexes.py`: restores sample indexes and creates the low-reasoning
  `contoso-company-kb-low` and minimal-reasoning `contoso-company-kb-minimal`.
- `infra/create-toolbox-foundryiq.py`: creates and promotes the toolbox version after the knowledge base exists. It uses the
  dedicated `kb-mcp-connection` remote-tool project connection by default and accepts overrides for the Work IQ
  knowledge-base toolbox.
- `infra/create-fabric-graph.py`: creates or updates the lakehouse-backed product review Graph Model, refreshes
  its queryable data, and writes its ID and Fabric portal URL to `.env`.
- `data/research-corpus.json`: the arXiv papers the research graph is built from, plus the two thresholds that
  decide how far its foundation and descendant tiers reach. This is the only file to edit to point the whole
  research pipeline at a different literature; every later step derives its scope from it.
- `infra/fetch-research-citations.py`: reads that corpus definition and caches citation and authorship edges from
  the keyless Semantic Scholar Graph API into `data/research-graph/citation-graph.json`. Accepts `--corpus`.
- `infra/create-research-graph.py`: loads that cached graph into `ResearchLiteratureLakehouse` and creates,
  refreshes, and records the `ResearchCitationGraph` Graph Model. It also extracts the entity layer — methods,
  tasks, datasets, metrics, venues, reported limitations and limitation themes — into 20 Delta tables that both
  IQ surfaces read.
- `infra/export-research-graph.py`: projects the two caches into the `{nodes, links}` graph the knowledge base and
  the explorer read, using `NODE_SPECS` and `EDGE_SPECS` from the graph builder. It builds offline rather than
  querying the Graph Model back, so the Foundry IQ half needs no Fabric capacity and `graph.json` stays a
  generated file rather than a committed one.
- `infra/create-research-knowledge-base.py`: the Foundry IQ half of the research corpus. Builds paper, limitation
  and theme cards from that projection, embeds them with `text-embedding-3-large`, and creates the
  `research-papers` index and the `research-literature-kb` knowledge base.
- `infra/create-research-ontology.py`: creates `ResearchLiteratureOntology`, a typed semantic layer over the same
  20 tables with its own `search_ontology` MCP endpoint. It imports `NODE_SPECS` and `EDGE_SPECS` from
  `infra/create-research-graph.py` rather than restating them, so the ontology and the Graph Model cannot drift.
  Creating the item generates a backing lakehouse and Graph Model; the first `refreshGraph` fails with
  `GraphNotRefreshable` and must be re-queued after the definition update lands.
- `infra/create-fabriciq-ontology-connection.py`: registers the single-tenant Entra application, grants consent,
  and writes the OAuth2 `RemoteTool` connection that lets an agent query an ontology. Fabric IQ is delegated-only,
  so this replaces the managed-identity connection the other Fabric scripts use. It reuses the Entra helpers in
  `infra/create-toolbox-workiq.py`.
- `infra/create-research-ontology-agent.py`: creates `agent-research-ontology`, which pairs the knowledge base
  with the ontology over that OAuth2 connection. It refuses to run unless the connection exists and is `OAuth2`.
- `infra/create-fabric-data-agent.py`: creates or reuses an ontology-, Graph-, and semantic-model-backed Fabric
  Data Agent, selects the semantic model tables, publishes its staging configuration through the Fabric REST API,
  and writes its ID and MCP endpoint to `.env`.
- `infra/create-web-analytics-lakehouse.py`: creates a separate lakehouse and loads deterministic website session,
  page-view, and dimensional data.
- `infra/create-semantic-model.py`: binds the source-controlled TMDL model to the web analytics lakehouse SQL
  endpoint, creates or updates the Direct Lake semantic model, and writes its ID and portal URL to `.env`.
- `infra/create-web-analytics-report.py`: binds the source-controlled PBIR definition to the semantic model,
  creates or updates the Power BI report, and writes its ID and portal URL to `.env`.
- `infra/create-toolbox-workiq.py`: opt-in Graph SDK setup for the Work IQ service principal, single-tenant Entra
  app, delegated consent, OAuth2 `RemoteA2A` connection, callback URI, and separate Work IQ toolbox.
- `infra/create-lakehouse.py`: creates optional Fabric lakehouse and ontology resources.
- `infra/setup-env.py`: writes generated Azure outputs to the local `.env` used by notebooks and local agent runs.
- `infra/hooks/postprovision.sh` and `infra/hooks/postprovision.ps1`: run after infrastructure provisioning to
  write local settings, seed Search, and optionally prepare Fabric.
- `infra/hooks/postdeploy.sh` and `infra/hooks/postdeploy.ps1`: run after agent deployment, resolve the generated
  hosted-agent identity, and grant it `Search Index Data Contributor` on Azure AI Search.
- `notebooks/fabriciq-dataagent-mcp.ipynb`: authenticates to the published Fabric Data Agent MCP endpoint, lists
  its tools, and submits a question through the selected tool.
- `notebooks/fabriciq-ontology-mcp.ipynb`: authenticates to the Fabric ontology MCP endpoint, lists its tools, and
  queries product and inventory data through `search_ontology`.
- `notebooks/foundryiq-mcp.ipynb`: authenticates to the minimal Azure AI Search knowledge-base MCP endpoint,
  lists its tool schema, and retrieves extractive HR and benefits passages through `knowledge_base_retrieve`.
- `notebooks/`: the six ordered Foundry IQ lab notebooks. Their extra kernel dependencies are listed in
  `notebooks/requirements.txt`.
- `src/agent-foundryiq-mcp/main.py`: Agent Framework application. It exposes a Responses server, uses Foundry for chat, and
  connects to the Search knowledge base with an authenticated `MCPStreamableHTTPTool`.
- `src/agent-foundryiq-api/main.py`: sibling Agent Framework application whose custom Python tool calls the Azure AI Search
  knowledge-base retrieval API with `KnowledgeBaseRetrievalClient`.
- `src/agent-toolbox-foundryiq/main.py`: sibling Agent Framework application that accesses the knowledge base,
  web search, and code interpreter through `FoundryToolbox`.
- `src/agent-toolbox-foundryiq-workiq/main.py`: standalone Agent Framework application that accesses the
  Work IQ-backed multi-source knowledge base through `FoundryToolbox`.
- `src/agent-toolbox-workiq/main.py`: sibling Agent Framework application that accesses the signed-in
  user's Microsoft 365 work context through the Work IQ toolbox.
- `src/agent-foundryiq-mcp/pyproject.toml`, `src/agent-foundryiq-mcp/uv.lock`, and `src/agent-foundryiq-mcp/uv.toml`: isolated MCP agent dependency
  definition, lockfile, and remote-build TLS configuration.
- `src/agent-foundryiq-api/pyproject.toml`, `src/agent-foundryiq-api/uv.lock`, and `src/agent-foundryiq-api/uv.toml`: isolated API agent
  dependency definition, lockfile, and remote-build TLS configuration.
- `src/agent-toolbox-foundryiq/pyproject.toml`, `src/agent-toolbox-foundryiq/uv.lock`, and
  `src/agent-toolbox-foundryiq/uv.toml`: isolated toolbox agent dependency definition, lockfile, and remote-build
  TLS configuration.
- `src/agent-toolbox-foundryiq-workiq/pyproject.toml`, `src/agent-toolbox-foundryiq-workiq/uv.lock`, and
  `src/agent-toolbox-foundryiq-workiq/uv.toml`: isolated Work IQ knowledge-base agent dependency definition,
  lockfile, and remote-build TLS configuration.
- `src/agent-toolbox-workiq/pyproject.toml`, `src/agent-toolbox-workiq/uv.lock`, and
  `src/agent-toolbox-workiq/uv.toml`: isolated Work IQ agent dependency definition, lockfile, and
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
- Do not commit the output of a script in `infra/`. `graph.json`, `map.json`, and the Atlas parquet are all
  rebuilt from `data/research-graph/`, so they are ignored rather than tracked.

The MCP server requires authentication during `session.initialize()`. Attach Azure bearer authentication to the
supplied `httpx.AsyncClient`; do not replace it with only `header_provider`, which applies runtime tool-call headers
too late for initialization.

## Research corpus artifacts

Two files under `data/research-graph/` are committed on purpose and everything else about the research graph is
derived from them:

| File | Written by | Why it is committed |
| --- | --- | --- |
| `citation-graph.json` | `infra/fetch-research-citations.py` | Minutes of crawling against an anonymous Semantic Scholar quota |
| `paper-entities.json` | `infra/extract-research-entities.py` | Non-deterministic model output; re-running changes the graph |

Both are marked `linguist-generated` in `.gitattributes` so they stay out of the reviewable diff without giving up
reproducibility. `raw-s2-edges.json` is a 16 MB intermediate and is ignored outright.

Everything downstream is regenerated, in this order, and none of it is tracked:

```bash
uv run python infra/export-research-graph.py   # -> web/research-explorer/graph.json
uv run python infra/build-research-map.py      # -> web/research-explorer/map.json
```

`export-research-graph.py` projects the caches directly rather than querying the Fabric Graph Model back. The
projection is driven by the same `NODE_SPECS` and `EDGE_SPECS` that populate Fabric, so the two cannot disagree,
and the Foundry IQ knowledge base can be built without provisioning a capacity at all.
`create-research-knowledge-base.py` calls that projection in memory, so it needs no file on disk.

## Fabric Data Agent SDK limitations

`infra/create-fabric-data-agent.py` intentionally uses the Fabric REST API with the root Python environment instead
of `fabric-data-agent-sdk`. Reconsider the SDK when both of these preview limitations are fixed:

- `fabric-data-agent-sdk==0.1.26a0` pins `azure-identity==1.17.1` and `httpx==0.27.2`, which conflicts with this
  repository's newer root dependencies.
- The SDK's public management objects resolve workspaces and item names through Semantic Link. Outside a Fabric
  notebook, create and reuse paths can initialize Semantic Link's .NET workspace client and fail with
  `RuntimeError: Can not determine dotnet root`.

The package metadata points to the internal `A365/SynapseML-Agent-SDK` Azure DevOps repository and does not publish
a public issue tracker. Check newer package metadata and release notes before restoring an SDK dependency.

The Fabric Data Agent MCP endpoint redirects requests through Fabric's routing layer. Its MCP client must use an
HTTP client with redirects enabled before `session.initialize()`; a plain `httpx.AsyncClient` defaults to
`follow_redirects=False` and can surface a misleading `500 Internal Server Error`. The current MCP client works and
negotiates the endpoint's supported protocol version when `follow_redirects=True`.

## Fabric GQL syntax constraints

Two constraints apply to every direct Graph GQL query, including those in
`notebooks/fabriciq-graph.ipynb` and `notebooks/fabriciq-research-graph.ipynb`:

- Use `FILTER`, not Cypher's `WHERE`. The endpoint frequently **times out** on `WHERE` instead of
  returning a syntax diagnostic, so the mistake presents as a hang.
- `GROUP BY` takes the `RETURN` alias, not the property path. `GROUP BY dst.title` fails with
  `mismatched input '.'`; write `RETURN dst.title AS paper ... GROUP BY paper`. There is no implicit
  grouping, so omitting `GROUP BY` raises *"the identifier cannot be used, as it is neither part of
  the GROUP BY nor an aggregation"*.

## Fabric Load Table type inference

The Load Table API infers column types from CSV content and exposes no `inferSchema` switch
(`Csv` accepts only `format`, `header`, and `delimiter`). Identifier columns that look numeric are
read as doubles and lose trailing zeros: a bare arXiv id `2404.16130` becomes `2404.1613`. This
happens even when the Graph Model `graphType` declares the property as `STRING`, and it silently
breaks equality filters rather than failing loudly. `infra/create-research-graph.py` prefixes arXiv
ids with `arXiv:` so the column cannot be typed as a number. Apply the same defence to any
identifier column made only of digits and dots.

## Attaching a lakehouse to a Fabric Data Agent

Every one of these failed silently rather than loudly, so check them in order when NL2SQL invents
column names or claims a table does not exist.

- Add the lakehouse as `{"type": "LakehouseTables", "lakehouseReference": {...}}`. A generic
  `FabricItem` datasource is *accepted* and looks healthy, but its element tree only ever exposes
  the `Schemas` and `Files` containers, NL2SQL never receives a schema, and the agent then
  **hallucinates column names** instead of reporting that it has none. The `SQLEndpoint` item is
  not an accepted datasource type at all — the long-running operation reports `Failed`.
- Walk the element tree with the **case-sensitive `rootId`** query parameter on the same
  `/elements` route, one level at a time, following `continuationToken`. There is no nested route.
  `parentId`, `parent`, `elementId` and `path` are all accepted and **silently ignored**, so a
  wrong guess is indistinguishable from an empty lakehouse. Element ids are base64 of the path, so
  `U2NoZW1hcw==` is `Schemas` and the leaves look like `Schemas/dbo/Tables/papers`.
- Selecting the `Schemas` container **does not cascade**. Only elements whose type is `Table`
  count, and each one needs its own `PATCH .../elements?id={tableId}` with `{"isSelected": true}`.
- The element list is a **snapshot taken when the datasource was added**. A lakehouse that gains
  tables afterwards keeps serving the stale list forever; the only fix is to delete and re-add the
  datasource. Detect this by comparing the element list against the tables you expect and re-adding
  the datasource when they diverge.
- Instructions and few-shot examples **do not substitute** for selected-table schema. A question
  that is verbatim one of the few-shots will still fail if no tables are selected.
- `GET /v1/workspaces/{ws}/lakehouses/{id}/tables` paginates. Always follow `continuationToken`, or
  you will conclude the lakehouse has only its first page of tables.
- `POST /v1/workspaces/{ws}/sqlEndpoints/{id}/refreshMetadata?preview=true` forces the SQL endpoint
  to pick up new Delta tables and returns a per-table sync report; `NotRun` means already current.

## Reaching Fabric from a Foundry agent

A Foundry project connection can call a Fabric Data Agent MCP endpoint directly. Use category
`RemoteTool`, `authType` `ProjectManagedIdentity` and audience `https://api.fabric.microsoft.com`,
then attach it to an agent as a `fabric_iq_preview` tool.

Fabric authorizes this against **workspace membership, not Azure RBAC**, so the Azure role
assignments the rest of this repo relies on are not enough. Add the Foundry *project's* principal to
the workspace with `POST /v1/workspaces/{ws}/roleAssignments` as a `ServicePrincipal`. Note that the
project identity and the account identity differ, and that a deployed agent additionally has its own
`instance_identity`; the connection uses the project identity.

Prompt agents (`"kind": "prompt"`) are created with `POST {projectEndpoint}/agents` and appear in the
portal agent playground without a container build. Invoke them through the Responses API with
`extra_body={"agent_reference": {"type": "agent_reference", "name": ...}}` — the older `agent`
property is rejected.

## Fabric Graph Model refreshes

Updating a Graph Model definition only queues a refresh when the **definition itself** changes.
Re-running a build script with new lakehouse data but an identical definition leaves the previously
ingested graph in place, and queries silently return stale values. Queue the refresh explicitly with
`POST /v1/workspaces/{workspaceId}/GraphModels/{graphId}/jobs/refreshGraph/instances`, then poll
`.../jobs/refreshGraph/instances` until the newest job reports `Completed`. A 20-node graph takes
roughly three minutes.

## Fabric capacity sizing

Scaling the capacity SKU is an in-place update that completes in seconds and preserves all data, but
it **recycles the compute**: immediately afterwards, graph GQL, ontology MCP, and both Data Agent
paths fail until they warm up again, which takes about 90 seconds. Never change the SKU shortly
before a demo, and re-run the preflight checks after any change.

## Open Fabric Data Agent issue

Fabric Data Agent NL2GQL can generate Cypher-style `WHERE` clauses for Fabric Graph queries, but the Fabric Graph
GQL endpoint requires `FILTER`. For example, a generated negative-sentiment review query timed out with `WHERE`,
while changing only `WHERE` to `FILTER` succeeded against the same graph. The Graph endpoint may time out on this
invalid syntax instead of returning a useful syntax diagnostic.

Removing the predicate is not a reliable workaround. Fabric Data Agent also failed to execute a generated
predicate-free aggregate query and a simple feature-list query, even though the exact generated aggregate GQL
succeeded through the Graph REST endpoint. An ontology-backed inventory question succeeded through the same
published Data Agent MCP tool, isolating the broader failure to the Data Agent's Graph source path rather than MCP
connectivity. Adding Graph-specific instructions and two validated direct-execution GQL examples as published
few-shot queries did not resolve either Graph question. Diagnostics confirmed that both examples loaded, matched
the predicate-free question, and guided NL2GQL to a valid query. The exact generated query succeeded through the
Graph REST endpoint, but the Data Agent's `analyze.database.execute` step failed with `Failed to execute GQL: Unable
to process the request` (RAID `bc80ee4e-8fc0-4fd0-b47f-8035b54c2c64`). Until the Graph integration is fixed, use
direct Graph GQL for review questions and ontology-backed questions for Data Agent demonstrations.

Ontology NL2GQL can also generate an invalid `ORDER BY` identifier even when it selects the correct source and
properties. One inventory query projected `quantityOnHand AS quantity_on_hand` but sorted by the undefined
`quantity_onHand`, failing with internal code `42000` and RAID `615b7b72-a632-4622-9c4c-65dcb2ed02db`.
Ontology-specific instructions to preserve exact property names and projected aliases repaired the query. Fabric
rejects attempts to add source examples with `Few shot examples are not supported for Ontology data sources`, so
keep ontology guidance instruction-based unless that API capability changes.

## Reaching a Fabric Ontology from a Foundry agent

Fabric IQ authenticates with Microsoft Entra delegated authentication (On-Behalf-Of). Every request runs as the
signed-in user and **application-only authentication is not supported**, so a `ProjectManagedIdentity` connection
can never reach an ontology. Using one produces `Failed to translate NL query to ontology query` for every
question that the same endpoint answers instantly for a user token. That error names authentication, not the
ontology, so it is easy to misread as an ontology or role problem. It is neither: `fabric_iq_preview` and a
generic `mcp` tool fail identically, `ContosoDIYOntology` fails the same way, fronting the ontology with a data
agent does not help because the data agent reads the ontology as its caller, and promoting the identity from
workspace `Member` to `Admin` changes nothing.

Use a bring-your-own Entra application wired as an OAuth2 connection instead.
`infra/create-fabriciq-ontology-connection.py` does this end to end: it registers a single-tenant application,
grants tenant-wide consent, issues a client secret, creates the OAuth2 `RemoteTool` connection against the
ontology MCP endpoint, and registers the returned OAuth callback on the application.

Two details differ from the [Fabric IQ tool documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq):

- The documented delegated permissions are `Item.Execute.All` and `Item.Read.All`, but the ontology endpoint
  rejects that pair with `401 ... Required scopes: Item.ReadWrite.All and Item.Execute.All`. Grant
  `Item.ReadWrite.All`. Trust the endpoint over the documentation.
- Consent is cached per connection. Changing the scopes on an existing connection does not re-prompt, so the
  stale token keeps failing. Create a connection under a new name to force a fresh consent.

The first call returns an `oauth_consent_request` output item carrying a `consent_link`. Open it once in a
browser; afterwards the agent answers normally. Because the token is the caller's, the agent sees exactly the
Fabric items that user can see, which is the point of the delegated model.

`agent-research-ontology` is the working example. It pairs the knowledge base with the ontology and routes
semantic questions to Foundry IQ (about 19s) and structural questions to Fabric IQ (about 43s).

## Fabric capacity pauses look like a broken endpoint

A paused capacity answers Fabric MCP endpoints with `HTTP 404` and
`Internal error CapacityNotActive.Capacity {id} is not active`. Foundry then reports the generic
`returned HTTP 404 (Not Found) while enumerating tools`, which reads like a wrong URL or an unpublished item, so
check the capacity before re-deriving endpoints:

```bash
az fabric capacity show --capacity-name <name> --resource-group <rg> --query state -o tsv
az fabric capacity resume --capacity-name <name> --resource-group <rg>
```

Resuming takes seconds, but the compute needs roughly 90 seconds afterwards before graph, ontology, and data
agent paths respond. `files/preflight.py` checks capacity state first for this reason.

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
uv run python -m compileall -q infra src/agent-foundryiq-mcp src/agent-foundryiq-api src/agent-toolbox-foundryiq src/agent-toolbox-foundryiq-workiq src/agent-toolbox-workiq
az bicep build --file infra/main.bicep --stdout > /dev/null
azd show
```

Validate the hosted-agent package separately:

```bash
uv sync --project src/agent-foundryiq-mcp --python 3.13 --frozen --dry-run
uv run --project src/agent-foundryiq-mcp --python 3.13 python -m py_compile src/agent-foundryiq-mcp/main.py
uv sync --project src/agent-foundryiq-api --python 3.13 --frozen --dry-run
uv run --project src/agent-foundryiq-api --python 3.13 python -m py_compile src/agent-foundryiq-api/main.py
uv sync --project src/agent-toolbox-foundryiq --python 3.13 --frozen --dry-run
uv run --project src/agent-toolbox-foundryiq --python 3.13 python -m py_compile src/agent-toolbox-foundryiq/main.py
uv sync --project src/agent-toolbox-foundryiq-workiq --python 3.13 --frozen --dry-run
uv run --project src/agent-toolbox-foundryiq-workiq --python 3.13 python -m py_compile src/agent-toolbox-foundryiq-workiq/main.py
uv sync --project src/agent-toolbox-workiq --python 3.13 --frozen --dry-run
uv run --project src/agent-toolbox-workiq --python 3.13 python -m py_compile src/agent-toolbox-workiq/main.py
```

Validate deployment hooks after editing them:

```bash
sh -n infra/hooks/postdeploy.sh
azd hooks run postdeploy
```

Use `azd hooks run postdeploy` to retry the postdeploy role-assignment step without rerunning provisioning or agent deployment. The hook uses `AzureDeveloperCliCredential` and requires the active azd environment to provide `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, and `AZURE_AI_SEARCH_SERVICE_NAME`. Confirm those values with `azd env get-value` before running the hook.

Run any agent locally with the same service manifest:

```bash
azd ai agent run agent-foundryiq-mcp
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-foundryiq-api
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-toolbox-foundryiq
azd ai agent invoke --local "What benefits are available, and when do I need to enroll?"

azd ai agent run agent-toolbox-workiq
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
azd deploy agent-foundryiq-mcp
azd ai agent invoke agent-foundryiq-mcp "What benefits are available, and when do I need to enroll?"

azd deploy agent-foundryiq-api
azd ai agent invoke agent-foundryiq-api "What benefits are available, and when do I need to enroll?"

azd deploy agent-toolbox-foundryiq
azd ai agent invoke agent-toolbox-foundryiq "What benefits are available, and when do I need to enroll?"

azd deploy agent-toolbox-foundryiq-workiq
azd ai agent invoke agent-toolbox-foundryiq-workiq --new-session --new-conversation \
  "Search my recent emails for Professional Claw Hammer and summarize requested actions."

azd deploy agent-toolbox-workiq
azd ai agent invoke agent-toolbox-workiq --new-session --new-conversation \
  "Check my recent Teams chats for messages about the Professional Claw Hammer. Summarize what colleagues are saying and what actions have been requested."
```

`azd up` performs these phases:

1. Bicep provisions shared Azure resources.
2. `postprovision` writes `.env`, restores Search data, creates the agent knowledge base and toolbox, and optionally
  configures Fabric.
3. Foundry remotely builds and deploys all agent packages under `src/`.
4. `postdeploy` grants Search data access to the Search-backed agents.
    The Work IQ agent additionally receives Foundry User at account and project scopes and uses the caller's
    delegated Microsoft 365 identity.

Do not move the hosted-agent role assignment into Bicep or `postprovision` unless the identity lifecycle changes.
The instance identity does not exist until the agent has been deployed.

## Deployment troubleshooting

### Remote build or TLS failure

Keep each agent's `uv.toml` with `system-certs = true`. Regenerate the corresponding lockfile with Python 3.13
after dependency changes:

```bash
uv lock --project src/agent-foundryiq-mcp --python 3.13
uv sync --project src/agent-foundryiq-mcp --python 3.13 --frozen --dry-run

uv lock --project src/agent-foundryiq-api --python 3.13
uv sync --project src/agent-foundryiq-api --python 3.13 --frozen --dry-run

uv lock --project src/agent-toolbox-foundryiq --python 3.13
uv sync --project src/agent-toolbox-foundryiq --python 3.13 --frozen --dry-run

uv lock --project src/agent-toolbox-foundryiq-workiq --python 3.13
uv sync --project src/agent-toolbox-foundryiq-workiq --python 3.13 --frozen --dry-run

uv lock --project src/agent-toolbox-workiq --python 3.13
uv sync --project src/agent-toolbox-workiq --python 3.13 --frozen --dry-run
```

### Missing runtime setting

Every required hosted variable must be listed under the corresponding agent's `environmentVariables` in
`azure.yaml`. Local `.env` values are not automatically available in the hosted container.

### MCP cancellation error

A message such as `MCP server failed to initialize: Cancelled via cancel scope` often masks the HTTP failure that
caused the MCP transport to close. Inspect the preceding `httpx` log line:

- `401 Unauthorized`: the initialization request did not carry a valid bearer token. Check the authenticated
  `httpx.AsyncClient` in `src/agent-foundryiq-mcp/main.py`.
- `403 Forbidden`: authentication succeeded, but the hosted agent's generated managed identity lacks Search RBAC
  or the role assignment has not propagated yet.

The `postdeploy` hook grants the required Search role. Azure RBAC may take a few minutes to propagate; retry in a
fresh invocation before changing application code.

### Inspect deployed sessions

List sessions and monitor the exact failing session rather than mixing logs from older versions:

```bash
azd ai agent sessions list --output json
azd ai agent monitor agent-foundryiq-mcp --session-id <session-id> --type console --tail 300 --utc
azd ai agent monitor agent-foundryiq-mcp --session-id <session-id> --type system --tail 300 --utc
```

Confirm the active deployment and its generated identity with:

```bash
azd ai agent show agent-foundryiq-mcp --output json
```

A healthy knowledge-base request should show successful Search MCP responses followed by
`knowledge_base_retrieve succeeded`.

## Azure CLI safety

Before provisioning, deploying, assigning roles, or monitoring, verify the selected Azure subscription, tenant,
and `azd` environment. Avoid sharing mutable Azure CLI or `azd` state across concurrent sessions. Prefer an isolated
CLI profile when automating commands, and always pass the intended `azd` environment explicitly when more than one
environment exists.
