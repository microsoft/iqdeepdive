"""Build a Fabric Graph Model over the research-literature corpus.

Fuses two caches into one queryable graph:

- infra/fetch-research-citations.py  the citation neighbourhood from Semantic Scholar
                                     (papers, authors, venues, citation intents)
- infra/extract-research-entities.py the semantic layer an LLM extracted from the
                                     indexed full text (methods, tasks, datasets,
                                     metrics, reported limitations)

The citation half answers "how do these papers relate to each other". The extracted
half answers "what is actually inside them", which is what turns the graph from a
bibliography into something a researcher can interrogate: method lineage, evaluation
practice, and combinations of techniques that nobody in the corpus has tried.

This is the structural counterpart to notebooks/foundryiq-research-literature.ipynb:
the knowledge base explains a paper, the graph situates it.

Environment variables (from .env):
  FABRIC_TENANT_ID                 - Microsoft Entra tenant ID for Fabric auth
  FABRIC_WORKSPACE_ID              - Existing Fabric workspace GUID
  RESEARCH_LAKEHOUSE_NAME          - Lakehouse name (default: ResearchLiteratureLakehouse)
  FABRIC_RESEARCH_GRAPH_NAME       - Graph Model name (default: ResearchCitationGraph)
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import time
import warnings
from pathlib import Path

import httpx
from azure.core.exceptions import (
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import AzureDeveloperCliCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv, set_key

warnings.filterwarnings(
    "ignore", category=SyntaxWarning, module=r"microsoft_fabric_api\..*"
)

from microsoft_fabric_api import FabricClient  # noqa: E402
from microsoft_fabric_api.generated.graphmodel.models import (  # noqa: E402
    CreateGraphModelRequest,
    GraphModelPublicDefinition,
    GraphModelPublicDefinitionPart,
    UpdateGraphModelDefinitionRequest,
)
from microsoft_fabric_api.generated.lakehouse.models import (  # noqa: E402
    CreateLakehouseRequest,
    Csv,
    LoadTableRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
CACHE_PATH = REPO_ROOT / "data" / "research-graph" / "citation-graph.json"
ENTITY_CACHE_PATH = REPO_ROOT / "data" / "research-graph" / "paper-entities.json"

load_dotenv(ENV_PATH, override=True)

ONELAKE_DFS_URL = "https://onelake.dfs.fabric.microsoft.com"
FABRIC_API_URL = "https://api.fabric.microsoft.com"

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
LAKEHOUSE_NAME = os.getenv("RESEARCH_LAKEHOUSE_NAME", "ResearchLiteratureLakehouse")
GRAPH_NAME = os.getenv("FABRIC_RESEARCH_GRAPH_NAME", "ResearchCitationGraph")
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://app.fabric.microsoft.com"
).rstrip("/")
REFRESH_TIMEOUT_SECONDS = 900

# The graph schema is declared once and drives the CSV upload, the Delta table load,
# and all four Graph Model definition parts.
NODE_SPECS = [
    {
        "alias": "paper_node",
        "label": "Paper",
        "table": "papers",
        "key": ("paperId", "paper_id"),
        "properties": [
            ("arxivId", "arxiv_id", "STRING"),
            ("title", "title", "STRING"),
            ("year", "year", "INT"),
            ("citationCount", "citation_count", "INT"),
            ("venue", "venue_name", "STRING"),
            ("tier", "tier", "STRING"),
            ("essence", "essence", "STRING"),
        ],
        "position": (520, 260),
    },
    {
        "alias": "author_node",
        "label": "Author",
        "table": "authors",
        "key": ("authorId", "author_id"),
        "properties": [("name", "name", "STRING")],
        "position": (160, 260),
    },
    {
        "alias": "venue_node",
        "label": "Venue",
        "table": "venues",
        "key": ("venueId", "venue_id"),
        "properties": [("name", "name", "STRING")],
        "position": (520, 40),
    },
    {
        "alias": "method_node",
        "label": "Method",
        "table": "methods",
        "key": ("methodId", "method_id"),
        "properties": [("name", "name", "STRING")],
        "position": (900, 100),
    },
    {
        "alias": "task_node",
        "label": "Task",
        "table": "tasks",
        "key": ("taskId", "task_id"),
        "properties": [("name", "name", "STRING")],
        "position": (900, 260),
    },
    {
        "alias": "dataset_node",
        "label": "Dataset",
        "table": "datasets",
        "key": ("datasetId", "dataset_id"),
        "properties": [("name", "name", "STRING")],
        "position": (900, 420),
    },
    {
        "alias": "metric_node",
        "label": "Metric",
        "table": "metrics",
        "key": ("metricId", "metric_id"),
        "properties": [("name", "name", "STRING")],
        "position": (900, 580),
    },
    {
        "alias": "limitation_node",
        "label": "Limitation",
        "table": "limitations",
        "key": ("limitationId", "limitation_id"),
        # 'text' is a reserved word in Fabric GQL and cannot be used as a property.
        "properties": [
            ("statement", "statement", "STRING"),
            ("theme", "theme", "STRING"),
        ],
        "position": (520, 580),
    },
    {
        "alias": "theme_node",
        "label": "Theme",
        "table": "themes",
        "key": ("themeId", "theme_id"),
        # Themes are the recurring problems behind individually-worded limitations.
        # Modelling them as nodes rather than a string property is what lets a query
        # ask which papers hit the same wall without matching on text.
        "properties": [
            ("name", "name", "STRING"),
            ("limitationCount", "limitation_count", "INT"),
        ],
        "position": (160, 580),
    },
]

EDGE_SPECS = [
    {
        "alias": "cites_edge",
        "label": "CITES",
        "table": "citations",
        "source": ("paper_node", "src_paper_id"),
        "destination": ("paper_node", "dst_paper_id"),
        "properties": [
            ("intent", "intent", "STRING"),
            ("isMethodological", "is_methodological", "BOOLEAN"),
            ("isInfluential", "is_influential", "BOOLEAN"),
        ],
    },
    {
        "alias": "authored_edge",
        "label": "AUTHORED",
        "table": "authorship",
        "source": ("author_node", "author_id"),
        "destination": ("paper_node", "paper_id"),
        "properties": [],
    },
    {
        "alias": "coauthored_edge",
        "label": "COAUTHORED",
        "table": "coauthorship",
        "source": ("author_node", "left_author_id"),
        "destination": ("author_node", "right_author_id"),
        "properties": [("papersTogether", "papers_together", "INT")],
    },
    {
        "alias": "published_in_edge",
        "label": "PUBLISHED_IN",
        "table": "publication",
        "source": ("paper_node", "paper_id"),
        "destination": ("venue_node", "venue_id"),
        "properties": [],
    },
    {
        "alias": "proposes_edge",
        "label": "PROPOSES",
        "table": "proposes",
        "source": ("paper_node", "paper_id"),
        "destination": ("method_node", "method_id"),
        "properties": [],
    },
    {
        "alias": "uses_edge",
        "label": "USES",
        "table": "uses",
        "source": ("paper_node", "paper_id"),
        "destination": ("method_node", "method_id"),
        "properties": [],
    },
    {
        "alias": "addresses_edge",
        "label": "ADDRESSES",
        "table": "addresses",
        "source": ("paper_node", "paper_id"),
        "destination": ("task_node", "task_id"),
        "properties": [],
    },
    {
        "alias": "evaluates_on_edge",
        "label": "EVALUATES_ON",
        "table": "evaluates_on",
        "source": ("paper_node", "paper_id"),
        "destination": ("dataset_node", "dataset_id"),
        "properties": [],
    },
    {
        "alias": "measures_edge",
        "label": "MEASURES",
        "table": "measures",
        "source": ("paper_node", "paper_id"),
        "destination": ("metric_node", "metric_id"),
        "properties": [],
    },
    {
        "alias": "reports_limitation_edge",
        "label": "REPORTS_LIMITATION",
        "table": "reported_limitations",
        "source": ("paper_node", "paper_id"),
        "destination": ("limitation_node", "limitation_id"),
        "properties": [],
    },
    {
        "alias": "has_theme_edge",
        "label": "HAS_THEME",
        "table": "limitation_themes",
        "source": ("limitation_node", "limitation_id"),
        "destination": ("theme_node", "theme_id"),
        "properties": [],
    },
]

TABLES = tuple(
    dict.fromkeys(
        [spec["table"] for spec in NODE_SPECS] + [spec["table"] for spec in EDGE_SPECS]
    )
)

_CREDENTIAL = None
_FABRIC_CLIENT = None


def require(value: str, name: str) -> str:
    """Return a required value or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to build the research citation graph.")
    return value


def get_credential() -> AzureDeveloperCliCredential:
    """Return the shared Fabric credential."""
    global _CREDENTIAL
    if _CREDENTIAL is None:
        _CREDENTIAL = AzureDeveloperCliCredential(tenant_id=FABRIC_TENANT_ID)
    return _CREDENTIAL


def get_fabric_client() -> FabricClient:
    """Return the shared Fabric SDK client."""
    global _FABRIC_CLIENT
    if _FABRIC_CLIENT is None:
        _FABRIC_CLIENT = FabricClient(get_credential())
    return _FABRIC_CLIENT


def normalize_arxiv_ids(papers: list[dict]) -> list[dict]:
    """Prefix arXiv ids so the CSV loader cannot type them as numbers.

    The Fabric Load Table API infers column types from CSV content and has no
    inferSchema switch. A bare id such as 2404.16130 is read as a double and loses
    its trailing zero, becoming 2404.1613, which silently breaks equality filters
    even though graphType declares the property as STRING.
    """
    return [
        {
            **paper,
            "arxiv_id": (
                f"arXiv:{paper['arxiv_id']}"
                if paper["arxiv_id"] and not paper["arxiv_id"].startswith("arXiv:")
                else paper["arxiv_id"]
            ),
        }
        for paper in papers
    ]


def entity_id(prefix: str, name: str) -> str:
    """Build a stable, never-numeric key for an extracted entity."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not slug:
        slug = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{slug}"[:120]


def build_tables(citation_graph: dict, entities: dict) -> dict[str, list[dict]]:
    """Fuse the citation neighbourhood and the extracted semantics into flat tables.

    Every table is keyed the way NODE_SPECS and EDGE_SPECS expect, so adding a
    concept means adding a spec entry and a table here and nothing else.
    """
    papers = normalize_arxiv_ids(citation_graph["papers"])
    by_arxiv = {
        paper["arxiv_id"].removeprefix("arXiv:"): paper
        for paper in papers
        if paper["arxiv_id"]
    }

    # The extracted layer only covers the core papers, whose full text is indexed.
    essence_by_paper_id: dict[str, str] = {}
    unmatched = []
    for record in entities["papers"].values():
        paper = by_arxiv.get(record["arxiv_id"])
        if paper is None:
            unmatched.append(record["arxiv_id"])
            continue
        essence_by_paper_id[paper["paper_id"]] = record["essence"]
        record["paper_id"] = paper["paper_id"]
    if unmatched:
        raise RuntimeError(
            "Extracted entities reference papers missing from the citation cache: "
            f"{', '.join(unmatched)}"
        )

    venue_ids = {paper["venue"]: entity_id("v", paper["venue"]) for paper in papers}

    tables: dict[str, list[dict]] = {
        "papers": [
            {
                "paper_id": paper["paper_id"],
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "year": paper["year"],
                "citation_count": paper["citation_count"],
                "venue_name": paper["venue"],
                "tier": paper["tier"],
                "essence": essence_by_paper_id.get(paper["paper_id"], ""),
            }
            for paper in papers
        ],
        "authors": citation_graph["authors"],
        "venues": [
            {"venue_id": venue_id, "name": name}
            for name, venue_id in sorted(venue_ids.items())
        ],
        "citations": citation_graph["citations"],
        "authorship": citation_graph["authorship"],
        "coauthorship": citation_graph["coauthorship"],
        "publication": [
            {"paper_id": paper["paper_id"], "venue_id": venue_ids[paper["venue"]]}
            for paper in papers
        ],
    }

    # Extracted entity nodes and the paper edges that point at them.
    entity_kinds = [
        ("methods", "method_id", "m", ["proposes", "uses"]),
        ("tasks", "task_id", "t", ["tasks"]),
        ("datasets", "dataset_id", "d", ["datasets"]),
        ("metrics", "metric_id", "x", ["metrics"]),
    ]
    for table_name, id_column, prefix, fields in entity_kinds:
        names = sorted(
            {
                value
                for record in entities["papers"].values()
                for field in fields
                for value in record[field]
            }
        )
        tables[table_name] = [
            {id_column: entity_id(prefix, name), "name": name} for name in names
        ]

    edge_fields = [
        ("proposes", "method_id", "m", "proposes"),
        ("uses", "method_id", "m", "uses"),
        ("addresses", "task_id", "t", "tasks"),
        ("evaluates_on", "dataset_id", "d", "datasets"),
        ("measures", "metric_id", "x", "metrics"),
    ]
    for table_name, id_column, prefix, field in edge_fields:
        tables[table_name] = sorted(
            {
                (record["paper_id"], entity_id(prefix, value))
                for record in entities["papers"].values()
                for value in record[field]
            }
        )
        tables[table_name] = [
            {"paper_id": paper_id, id_column: target}
            for paper_id, target in tables[table_name]
        ]

    # Reported limitations are free text, so they are deduplicated by content hash.
    limitations: dict[str, dict] = {}
    reported: list[dict] = []
    for record in entities["papers"].values():
        for item in record["limitations"]:
            statement = " ".join(item["statement"].split())
            if not statement:
                continue
            key = "l_" + hashlib.sha1(statement.encode("utf-8")).hexdigest()[:16]
            limitations[key] = {
                "limitation_id": key,
                "statement": statement,
                "theme": item["theme"],
            }
            reported.append({"paper_id": record["paper_id"], "limitation_id": key})
    tables["limitations"] = [limitations[key] for key in sorted(limitations)]
    tables["reported_limitations"] = reported

    # Themes become their own nodes so limitations that describe the same underlying
    # problem are connected through it. Without this the limitation layer is a set of
    # disconnected stars and the shared failure modes are invisible.
    theme_members: dict[str, list[str]] = {}
    for record in tables["limitations"]:
        theme_members.setdefault(record["theme"], []).append(record["limitation_id"])
    tables["themes"] = [
        {
            "theme_id": entity_id("h", name),
            "name": name,
            "limitation_count": len(members),
        }
        for name, members in sorted(theme_members.items())
    ]
    tables["limitation_themes"] = [
        {"limitation_id": limitation_id, "theme_id": entity_id("h", name)}
        for name, members in sorted(theme_members.items())
        for limitation_id in sorted(members)
    ]

    missing = [table for table in TABLES if table not in tables]
    if missing:
        raise RuntimeError(f"Schema declares tables with no data builder: {missing}")
    return tables


def to_csv_bytes(rows: list[dict]) -> bytes:
    """Convert a list of dicts to CSV bytes.

    Values are flattened first: the Fabric Load Table API exposes no quote or
    multiline option, so an embedded newline in an extracted sentence would split
    one record across two rows. Booleans are lowercased because Spark's CSV type
    inference recognizes 'true'/'false'.
    """
    if not rows:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(
        {
            key: str(value).lower()
            if isinstance(value, bool)
            else " ".join(str(value).split())
            if isinstance(value, str)
            else value
            for key, value in row.items()
        }
        for row in rows
    )
    return output.getvalue().encode("utf-8")


def find_item_by_name(items, display_name: str):
    """Return the item with the requested display name, if present."""
    return next((item for item in items if item.display_name == display_name), None)


def ensure_lakehouse(workspace_id: str, name: str):
    """Create the lakehouse, or return the existing one."""
    existing = find_item_by_name(
        get_fabric_client().lakehouse.items.list_lakehouses(workspace_id), name
    )
    if existing is not None:
        print(f"Using existing lakehouse '{name}' ({existing.id})")
        return existing
    print(f"Creating lakehouse '{name}'...")
    poller = get_fabric_client().lakehouse.items.begin_create_lakehouse(
        workspace_id, CreateLakehouseRequest(display_name=name)
    )
    lakehouse = poller.result if not callable(poller.result) else poller.result()
    print(f"Created lakehouse '{name}' ({lakehouse.id})")
    return lakehouse


def upload_to_onelake(
    workspace_id: str, lakehouse_id: str, filename: str, data: bytes
) -> None:
    """Upload a CSV into the lakehouse Files section."""
    service_client = DataLakeServiceClient(
        account_url=ONELAKE_DFS_URL, credential=get_credential()
    )
    directory_client = service_client.get_file_system_client(
        workspace_id
    ).get_directory_client(f"{lakehouse_id}/Files")
    directory_client.get_file_client(filename).upload_data(data, overwrite=True)
    print(f"  uploaded {filename} ({len(data):,} bytes)")


def load_table(workspace_id: str, lakehouse_id: str, table_name: str) -> None:
    """Load an uploaded CSV into a Delta table.

    Eighteen sequential loads occasionally hit a dropped connection, so the call is
    retried; Overwrite mode makes it safe to repeat.
    """
    for attempt in range(4):
        try:
            get_fabric_client().lakehouse.tables.begin_load_table(
                workspace_id,
                lakehouse_id,
                table_name,
                LoadTableRequest(
                    relative_path=f"Files/{table_name}.csv",
                    path_type="File",
                    mode="Overwrite",
                    format_options=Csv(header=True, delimiter=","),
                ),
            ).result()
            print(f"  loaded table '{table_name}'")
            return
        except (ServiceRequestError, ServiceResponseError) as error:
            if attempt == 3:
                raise
            print(f"  retrying '{table_name}' after transport error: {error}")
            time.sleep(10 * (attempt + 1))


def definition_part(path: str, payload: dict) -> GraphModelPublicDefinitionPart:
    """Encode one public Graph Model definition part."""
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return GraphModelPublicDefinitionPart(
        path=path, payload=encoded, payload_type="InlineBase64"
    )


def build_definition(
    workspace_id: str, lakehouse_id: str
) -> GraphModelPublicDefinition:
    """Build the public definition from NODE_SPECS and EDGE_SPECS."""
    data_sources = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/dataSources/1.1.0/schema.json"
        ),
        "itemReferences": [
            {
                "name": "research_lakehouse",
                "item": {"workspaceId": workspace_id, "itemId": lakehouse_id},
            }
        ],
        "dataSources": [
            {
                "name": f"{table}_table",
                "type": "DeltaTable",
                "properties": {
                    "referenceName": "research_lakehouse",
                    "path": f"Tables/{table}",
                },
            }
            for table in TABLES
        ],
    }
    graph_type = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/graphType/1.0.0/schema.json"
        ),
        "nodeTypes": [
            {
                "alias": spec["alias"],
                "labels": [spec["label"]],
                "primaryKeyProperties": [spec["key"][0]],
                "properties": [{"name": spec["key"][0], "type": "STRING"}]
                + [
                    {"name": name, "type": kind}
                    for name, _, kind in spec["properties"]
                ],
            }
            for spec in NODE_SPECS
        ],
        "edgeTypes": [
            {
                "alias": spec["alias"],
                "labels": [spec["label"]],
                "sourceNodeType": {"alias": spec["source"][0]},
                "destinationNodeType": {"alias": spec["destination"][0]},
                "properties": [
                    {"name": name, "type": kind}
                    for name, _, kind in spec["properties"]
                ],
            }
            for spec in EDGE_SPECS
        ],
    }
    graph_definition = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/graphDefinition/1.0.0/schema.json"
        ),
        "nodeTables": [
            {
                "id": f"{spec['alias']}_mapping",
                "nodeTypeAlias": spec["alias"],
                "dataSourceName": f"{spec['table']}_table",
                "propertyMappings": [
                    {"propertyName": spec["key"][0], "sourceColumn": spec["key"][1]}
                ]
                + [
                    {"propertyName": name, "sourceColumn": column}
                    for name, column, _ in spec["properties"]
                ],
            }
            for spec in NODE_SPECS
        ],
        "edgeTables": [
            {
                "id": f"{spec['alias']}_mapping",
                "edgeTypeAlias": spec["alias"],
                "dataSourceName": f"{spec['table']}_table",
                "sourceNodeKeyColumns": [spec["source"][1]],
                "destinationNodeKeyColumns": [spec["destination"][1]],
                "propertyMappings": [
                    {"propertyName": name, "sourceColumn": column}
                    for name, column, _ in spec["properties"]
                ],
            }
            for spec in EDGE_SPECS
        ],
    }
    positions = {
        spec["alias"]: {"x": spec["position"][0], "y": spec["position"][1]}
        for spec in NODE_SPECS
    }
    style_aliases = [*positions] + [spec["alias"] for spec in EDGE_SPECS]
    styling = {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/item/"
            "graphIndex/definition/stylingConfiguration/1.0.0/schema.json"
        ),
        "modelLayout": {
            "positions": positions,
            "styles": {alias: {"size": 30} for alias in style_aliases},
            "pan": {"x": 0, "y": 0},
            "zoomLevel": 1,
        },
    }
    return GraphModelPublicDefinition(
        format="json",
        parts=[
            definition_part("dataSources.json", data_sources),
            definition_part("graphDefinition.json", graph_definition),
            definition_part("graphType.json", graph_type),
            definition_part("stylingConfiguration.json", styling),
        ],
    )


def list_refresh_jobs(workspace_id: str, graph_id: str) -> list[dict]:
    """List refresh jobs for a Graph Model."""
    token = get_credential().get_token(f"{FABRIC_API_URL}/.default").token
    response = httpx.get(
        f"{FABRIC_API_URL}/v1/workspaces/{workspace_id}/GraphModels/"
        f"{graph_id}/jobs/refreshGraph/instances",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("value", [])


def is_graph_queryable(workspace_id: str, graph_id: str) -> bool:
    """Return whether Fabric exposes a queryable graph type."""
    token = get_credential().get_token(f"{FABRIC_API_URL}/.default").token
    response = httpx.get(
        f"{FABRIC_API_URL}/v1/workspaces/{workspace_id}/GraphModels/"
        f"{graph_id}/getQueryableGraphType?preview=true",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    return response.status_code == 200


def trigger_refresh(workspace_id: str, graph_id: str) -> None:
    """Explicitly queue a graph refresh.

    Updating the definition only triggers a refresh when the definition itself changes.
    Re-running with new lakehouse data but an identical definition leaves the previously
    ingested graph in place, so the refresh is requested explicitly here.
    """
    token = get_credential().get_token(f"{FABRIC_API_URL}/.default").token
    response = httpx.post(
        f"{FABRIC_API_URL}/v1/workspaces/{workspace_id}/GraphModels/"
        f"{graph_id}/jobs/refreshGraph/instances",
        headers={"Authorization": f"Bearer {token}"},
        json={},
        timeout=60,
    )
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Could not queue graph refresh: {response.status_code} {response.text[:200]}"
        )


def wait_for_new_refresh(
    workspace_id: str,
    graph_id: str,
    existing_job_ids: set[str],
    active_job_ids: set[str],
) -> None:
    """Wait for the refresh initiated by a definition update."""
    started_at = time.monotonic()
    deadline = started_at + REFRESH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        relevant_jobs = [
            job
            for job in list_refresh_jobs(workspace_id, graph_id)
            if job["id"] not in existing_job_ids or job["id"] in active_job_ids
        ]
        if any(job["status"] in {"Completed", "Succeeded"} for job in relevant_jobs):
            return
        failed_jobs = [job for job in relevant_jobs if job["status"] == "Failed"]
        if failed_jobs and not any(
            job["status"] in {"NotStarted", "InProgress"} for job in relevant_jobs
        ):
            raise RuntimeError(
                f"Fabric Graph refresh failed: {failed_jobs[0].get('failureReason')}"
            )
        if (
            not relevant_jobs
            and time.monotonic() - started_at >= 30
            and is_graph_queryable(workspace_id, graph_id)
        ):
            return
        time.sleep(5)
    raise TimeoutError("Timed out waiting for the Fabric Graph refresh to complete.")


def main() -> None:
    """Load the citation corpus and build its queryable Graph Model."""
    require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    for path, producer in (
        (CACHE_PATH, "infra/fetch-research-citations.py"),
        (ENTITY_CACHE_PATH, "infra/extract-research-entities.py"),
    ):
        if not path.exists():
            raise RuntimeError(
                f"{path.relative_to(REPO_ROOT)} not found. Run {producer} first."
            )

    tables = build_tables(
        json.loads(CACHE_PATH.read_text(encoding="utf-8")),
        json.loads(ENTITY_CACHE_PATH.read_text(encoding="utf-8")),
    )
    print("Graph contents:")
    for table in TABLES:
        print(f"  {table:22s} {len(tables[table]):6d} rows")

    try:
        lakehouse = ensure_lakehouse(workspace_id, LAKEHOUSE_NAME)

        print("Uploading CSVs to OneLake...")
        for table in TABLES:
            upload_to_onelake(
                workspace_id,
                lakehouse.id,
                f"{table}.csv",
                to_csv_bytes(tables[table]),
            )

        print("Loading Delta tables...")
        for table in TABLES:
            try:
                load_table(workspace_id, lakehouse.id, table)
            except HttpResponseError as error:
                raise RuntimeError(f"Load failed for '{table}': {error}") from error

        definition = build_definition(workspace_id, lakehouse.id)
        graph = find_item_by_name(
            get_fabric_client().graphmodel.items.list_graph_models(workspace_id),
            GRAPH_NAME,
        )
        if graph is None:
            print(f"Creating Fabric Graph Model '{GRAPH_NAME}'...")
            graph = get_fabric_client().graphmodel.items.create_graph_model(
                workspace_id,
                CreateGraphModelRequest(
                    display_name=GRAPH_NAME,
                    description=(
                        "Citation, authorship, and extracted-semantics graph over the "
                        "retrieval-augmented generation research corpus."
                    ),
                ),
            )

        existing_jobs = list_refresh_jobs(workspace_id, graph.id)
        existing_job_ids = {job["id"] for job in existing_jobs}
        active_job_ids = {
            job["id"]
            for job in existing_jobs
            if job["status"] in {"NotStarted", "InProgress"}
        }

        print(f"Saving Graph Model definition for '{GRAPH_NAME}'...")
        get_fabric_client().graphmodel.items.begin_update_graph_model_definition(
            workspace_id,
            graph.id,
            UpdateGraphModelDefinitionRequest(definition=definition),
        ).result()

        print("Queueing the Graph Model refresh...")
        trigger_refresh(workspace_id, graph.id)

        print("Waiting for the Graph Model refresh...")
        wait_for_new_refresh(
            workspace_id, graph.id, existing_job_ids, active_job_ids
        )
    finally:
        if _CREDENTIAL is not None:
            _CREDENTIAL.close()

    graph_ui_url = (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/graph/{graph.id}"
        "?experience=fabric-developer"
    )
    for key, value in {
        "FABRIC_RESEARCH_GRAPH_ID": graph.id,
        "FABRIC_RESEARCH_GRAPH_NAME": GRAPH_NAME,
        "FABRIC_RESEARCH_GRAPH_UI_URL": graph_ui_url,
        "RESEARCH_LAKEHOUSE_NAME": LAKEHOUSE_NAME,
        "RESEARCH_LAKEHOUSE_ID": lakehouse.id,
    }.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"\nFabric Graph Model: {GRAPH_NAME} ({graph.id})")
    print(f"Fabric UI: {graph_ui_url}")


if __name__ == "__main__":
    main()
