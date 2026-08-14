"""Build the research graph JSON that the knowledge base and the explorer read.

The graph is assembled offline from the two cached inputs:

- ``data/research-graph/citation-graph.json``  (infra/fetch-research-citations.py)
- ``data/research-graph/paper-entities.json``  (infra/extract-research-entities.py)

Those are the same two caches that ``infra/create-research-graph.py`` loads into Fabric,
so projecting them here produces the content a GQL export of the Graph Model would return
without requiring a provisioned capacity. The Foundry IQ half of the sample can therefore
be built on its own, and the result stays reproducible instead of being committed.

Nodes and edges are projected straight from ``NODE_SPECS`` and ``EDGE_SPECS``, so a
concept added to the graph builder shows up here with no change to this file.

Usage:
    uv run python infra/export-research-graph.py
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO_ROOT / "web" / "research-explorer" / "graph.json"

# GQL reserves `label`, so node display text is exported as `name`.
# Colours are assigned here rather than in the browser so the notebook figures and the
# explorer can agree on what a Method or a Limitation looks like.
NODE_TYPES = {
    "Paper": {"color": "#d1495b", "group": "corpus"},
    "Author": {"color": "#8d99ae", "group": "people"},
    "Venue": {"color": "#5f6c7b", "group": "people"},
    "Method": {"color": "#3d84c6", "group": "semantic"},
    "Task": {"color": "#00a6a6", "group": "semantic"},
    "Dataset": {"color": "#6a994e", "group": "semantic"},
    "Metric": {"color": "#bc9820", "group": "semantic"},
    "Limitation": {"color": "#9b5de5", "group": "semantic"},
}

# Themes are real nodes in the Graph Model and the ontology, but the explorer shows them
# as a property of the limitation they group, so they are not exported as nodes here.
# Any edge touching an unexported node type is dropped with them.
DISPLAY_PROPERTY = {"Paper": "title", "Limitation": "statement"}

# Authors and co-authorship are excluded from the explorer's default view: 1,077 author
# nodes and 1,484 co-authorship edges bury the structure that makes the graph worth
# looking at. They stay in the export so the toggle can reveal them on demand.
DEFAULT_HIDDEN_NODE_TYPES = ["Author", "Venue"]
DEFAULT_HIDDEN_EDGE_TYPES = ["COAUTHORED", "AUTHORED", "PUBLISHED_IN"]


def load_graph_module():
    """Import the graph builder for its specs, table builder, and cache paths.

    The module's filename is not a valid Python identifier, so it is loaded by path.
    Importing it rather than restating the schema is the point: the exported graph and
    the Fabric Graph Model then describe the same entities by construction.
    """
    module_path = Path(__file__).with_name("create-research-graph.py")
    spec = importlib.util.spec_from_file_location("research_graph_builder", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load the graph builder from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def clean(row: dict) -> dict:
    """Drop empty values so the exported JSON stays small and the UI can test presence."""
    return {k: v for k, v in row.items() if v not in (None, "", "null")}


def build_nodes(
    node_specs: list[dict], tables: dict[str, list[dict]], verbose: bool = True
) -> list[dict]:
    """Project the node tables into explorer nodes, one entry per spec."""
    nodes: list[dict] = []
    for spec in node_specs:
        label = spec["label"]
        if label not in NODE_TYPES:
            continue
        display = DISPLAY_PROPERTY.get(label, "name")
        key_column = spec["key"][1]
        for row in tables[spec["table"]]:
            projected = {"id": row[key_column]}
            for gql_name, column, _type in spec["properties"]:
                target = "name" if gql_name == display else gql_name
                projected[target] = row.get(column)
            node = clean(projected)
            node["type"] = label
            nodes.append(node)
        if verbose:
            print(f"  {label:<12} {len(tables[spec['table']]):>5} nodes")
    return nodes


def build_links(
    edge_specs: list[dict],
    node_specs: list[dict],
    tables: dict[str, list[dict]],
    known_ids: set[str],
    verbose: bool = True,
) -> list[dict]:
    """Project the edge tables, dropping edges whose endpoints were not exported."""
    label_by_alias = {spec["alias"]: spec["label"] for spec in node_specs}
    links: list[dict] = []
    for spec in edge_specs:
        source_alias, source_column = spec["source"]
        destination_alias, destination_column = spec["destination"]
        endpoints = (
            label_by_alias[source_alias],
            label_by_alias[destination_alias],
        )
        if any(label not in NODE_TYPES for label in endpoints):
            continue
        kept = 0
        for row in tables[spec["table"]]:
            source, target = row[source_column], row[destination_column]
            if source not in known_ids or target not in known_ids:
                continue
            properties = clean(
                {
                    gql_name: row.get(column)
                    for gql_name, column, _type in spec["properties"]
                }
            )
            links.append(
                {
                    "source": source,
                    "target": target,
                    **properties,
                    "type": spec["label"],
                }
            )
            kept += 1
        if verbose:
            print(f"  {spec['label']:<20} {kept:>5} edges")
    return links


def build_graph(verbose: bool = True) -> dict:
    """Assemble the full graph payload from the cached corpus.

    Callers that only need the nodes and edges, such as the knowledge base builder, can
    use this directly instead of reading a previously exported file.
    """
    # Only used to stamp the portal ids into the payload; the graph itself is offline.
    load_dotenv(REPO_ROOT / ".env", override=True)

    graph = load_graph_module()
    for path in (graph.CACHE_PATH, graph.ENTITY_CACHE_PATH):
        if not path.exists():
            raise SystemExit(
                f"{path.relative_to(REPO_ROOT)} not found. Run "
                "infra/fetch-research-citations.py and "
                "infra/extract-research-entities.py first."
            )

    citation_graph = json.loads(graph.CACHE_PATH.read_text(encoding="utf-8"))
    entities = json.loads(graph.ENTITY_CACHE_PATH.read_text(encoding="utf-8"))
    tables = graph.build_tables(citation_graph, entities)

    if verbose:
        print("Building the research graph from the cached corpus\n")
    nodes = build_nodes(graph.NODE_SPECS, tables, verbose)
    known_ids = {node["id"] for node in nodes}
    links = build_links(graph.EDGE_SPECS, graph.NODE_SPECS, tables, known_ids, verbose)

    # Degree drives node size in the explorer. Computing it here keeps the browser's
    # startup work to rendering rather than counting.
    degree: dict[str, int] = dict.fromkeys(known_ids, 0)
    for link in links:
        degree[link["source"]] += 1
        degree[link["target"]] += 1
    for node in nodes:
        node["degree"] = degree[node["id"]]

    node_counts: dict[str, int] = {}
    edge_counts: dict[str, int] = {}
    for node in nodes:
        node_counts[node["type"]] = node_counts.get(node["type"], 0) + 1
    for link in links:
        edge_counts[link["type"]] = edge_counts.get(link["type"], 0) + 1

    return {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "workspaceId": os.getenv("FABRIC_WORKSPACE_ID", ""),
        "graphId": os.getenv("FABRIC_RESEARCH_GRAPH_ID", ""),
        "nodeTypes": NODE_TYPES,
        "defaultHiddenNodeTypes": DEFAULT_HIDDEN_NODE_TYPES,
        "defaultHiddenEdgeTypes": DEFAULT_HIDDEN_EDGE_TYPES,
        "stats": {
            "nodes": len(nodes),
            "links": len(links),
            "nodeCounts": node_counts,
            "edgeCounts": edge_counts,
        },
        "nodes": nodes,
        "links": links,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Where to write the graph JSON (default: web/research-explorer/graph.json)",
    )
    args = parser.parse_args()

    payload = build_graph()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    size_kb = args.output.stat().st_size / 1024
    location = args.output
    if location.is_relative_to(REPO_ROOT):
        location = location.relative_to(REPO_ROOT)
    print(
        f"\nWrote {location} "
        f"({payload['stats']['nodes']} nodes, {payload['stats']['links']} links, "
        f"{size_kb:.0f} KB)"
    )


if __name__ == "__main__":
    main()
