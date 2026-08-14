"""Create the Foundry IQ knowledge base over the RAG research corpus.

Every document is assembled from the cached corpus in data/research-graph/, projected
into nodes and edges by infra/export-research-graph.py. That is the same projection
infra/create-research-graph.py loads into Fabric, so this knowledge base and the Fabric
surfaces answer from one corpus: this is the semantic view of the papers, those are the
structured view of the network. Building here rather than reading the Graph Model back
means the Foundry IQ half needs no Fabric capacity.

Three document shapes, because researchers ask three different kinds of question:

  paper     one self-contained card per paper, so "what is X about" retrieves cleanly
  limits    one card per reported limitation, so "known weaknesses of X" ranks the
            limitation itself rather than burying it inside a long paper card
  theme     a roll-up per limitation theme, so "recurring problems in RAG" can be
            answered without the model having to aggregate 95 snippets itself
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from azure.identity.aio import AzureDeveloperCliCredential, get_bearer_token_provider
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    KnowledgeBase,
    KnowledgeBaseAzureOpenAIModel,
    KnowledgeSourceReference,
    SearchIndex,
    SearchIndexFieldReference,
    SearchIndexKnowledgeSource,
    SearchIndexKnowledgeSourceParameters,
)
from azure.search.documents.knowledgebases.models import (
    KnowledgeRetrievalLowReasoningEffort,
    KnowledgeRetrievalOutputMode,
)
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

load_dotenv(dotenv_path=".env", override=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = REPO_ROOT / "data" / "index-data" / "index.json"

INDEX_NAME = "research-papers"
KB_NAME = "research-literature-kb"
EMBED_BATCH = 96
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def load_graph() -> dict[str, Any]:
    """Build the corpus graph in memory from the cached inputs.

    The exporter's filename is not a valid Python identifier, so it is loaded by path.
    Calling it rather than reading a committed graph.json keeps the knowledge base and
    the Fabric graph derived from the same two caches, and means the file never has to
    be checked in.
    """
    module_path = Path(__file__).with_name("export-research-graph.py")
    spec = importlib.util.spec_from_file_location("research_graph_export", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the graph exporter from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.build_graph(verbose=False)


def build_documents(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the graph into retrievable cards."""
    nodes = {n["id"]: n for n in graph["nodes"]}

    # Outgoing edges keyed by (source, type) and the reverse for citation counts.
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    cited_by: dict[str, list[str]] = defaultdict(list)
    authors_of: dict[str, list[str]] = defaultdict(list)
    for link in graph["links"]:
        src, dst, rel = link["source"], link["target"], link["type"]
        out[(src, rel)].append(dst)
        if rel == "CITES":
            cited_by[dst].append(src)
        elif rel == "AUTHORED":
            authors_of[dst].append(src)

    def names(node_id: str, rel: str) -> list[str]:
        return sorted(
            nodes[t]["name"] for t in out.get((node_id, rel), []) if t in nodes
        )

    docs: list[dict[str, Any]] = []

    papers = [n for n in graph["nodes"] if n["type"] == "Paper"]
    for paper in papers:
        pid = paper["id"]
        author_names = sorted(nodes[a]["name"] for a in authors_of.get(pid, []) if a in nodes)
        venue = names(pid, "PUBLISHED_IN")
        proposes, uses = names(pid, "PROPOSES"), names(pid, "USES")
        tasks, datasets = names(pid, "ADDRESSES"), names(pid, "EVALUATES_ON")
        metrics, limits = names(pid, "MEASURES"), names(pid, "REPORTS_LIMITATION")

        lines = [f"# {paper['name']}"]
        meta = [str(paper.get("year") or "")]
        if venue:
            meta.append(venue[0])
        if paper.get("arxivId"):
            meta.append(paper["arxivId"])
        meta.append(f"{paper.get('citationCount', 0):,} citations worldwide")
        meta.append(f"cited by {len(cited_by.get(pid, []))} papers in this corpus")
        meta.append(f"{paper.get('tier', 'related')} paper")
        lines.append(" · ".join(m for m in meta if m))

        if author_names:
            shown = ", ".join(author_names[:8])
            extra = f" and {len(author_names) - 8} others" if len(author_names) > 8 else ""
            lines.append(f"\nAuthors: {shown}{extra}.")
        if paper.get("essence"):
            lines.append(f"\n{paper['essence']}")
        for label, values in (
            ("Proposes", proposes),
            ("Builds on methods", uses),
            ("Addresses tasks", tasks),
            ("Evaluates on datasets", datasets),
            ("Reports metrics", metrics),
        ):
            if values:
                lines.append(f"{label}: {', '.join(values)}.")
        if limits:
            lines.append("\nReported limitations:")
            lines.extend(f"- {text}" for text in limits)

        docs.append(
            {
                "uid": f"paper-{pid}",
                "snippet_parent_id": pid,
                "blob_path": f"/papers/{paper.get('arxivId') or pid} {paper['name']}",
                "snippet": "\n".join(lines),
            }
        )

    # One card per limitation. The paper card already lists them, but a dedicated card
    # lets a question about weaknesses rank the specific sentence instead of a whole paper.
    reported_by: dict[str, list[str]] = defaultdict(list)
    for link in graph["links"]:
        if link["type"] == "REPORTS_LIMITATION":
            reported_by[link["target"]].append(link["source"])

    for node in graph["nodes"]:
        if node["type"] != "Limitation":
            continue
        sources = [nodes[p]["name"] for p in reported_by.get(node["id"], []) if p in nodes]
        lines = [f"# Reported limitation: {node['name']}"]
        if node.get("theme"):
            lines.append(f"Theme: {node['theme']}.")
        if sources:
            lines.append(f"Reported by: {'; '.join(sorted(sources))}.")
        docs.append(
            {
                "uid": f"limitation-{node['id']}",
                "snippet_parent_id": node["id"],
                "blob_path": f"/limitations/{node.get('theme') or 'general'}",
                "snippet": "\n".join(lines),
            }
        )

    # Theme roll-ups, so "what keeps going wrong in this field" is one retrieval.
    by_theme: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for node in graph["nodes"]:
        if node["type"] != "Limitation" or not node.get("theme"):
            continue
        sources = [nodes[p]["name"] for p in reported_by.get(node["id"], []) if p in nodes]
        by_theme[node["theme"]].append((node["name"], sorted(sources)))

    for theme, entries in sorted(by_theme.items()):
        papers_hit = sorted({p for _, srcs in entries for p in srcs})
        lines = [
            f"# Recurring limitation theme: {theme}",
            f"{len(entries)} distinct limitations across {len(papers_hit)} papers "
            "in the retrieval-augmented generation corpus.",
            "",
            "Specific limitations:",
        ]
        lines.extend(f"- {text}" for text, _ in sorted(entries))
        if papers_hit:
            lines.append(f"\nPapers reporting this theme: {', '.join(papers_hit)}.")
        docs.append(
            {
                "uid": f"theme-{abs(hash(theme)) & 0xFFFFFFFF:08x}",
                "snippet_parent_id": f"theme::{theme}",
                "blob_path": f"/themes/{theme}",
                "snippet": "\n".join(lines),
            }
        )

    return docs


async def embed(
    docs: list[dict[str, Any]], openai_endpoint: str, deployment: str, credential: Any
) -> None:
    """Attach a snippet_vector to every document, in place."""
    token_provider = get_bearer_token_provider(credential, COGNITIVE_SCOPE)
    async with AsyncAzureOpenAI(
        azure_endpoint=openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    ) as client:
        for start in range(0, len(docs), EMBED_BATCH):
            chunk = docs[start : start + EMBED_BATCH]
            response = await client.embeddings.create(
                model=deployment, input=[d["snippet"] for d in chunk]
            )
            for doc, item in zip(chunk, response.data, strict=True):
                doc["snippet_vector"] = item.embedding
            print(f"  embedded {min(start + EMBED_BATCH, len(docs))}/{len(docs)}")


async def create_index(
    endpoint: str, credential: Any, openai_endpoint: str, deployment: str
) -> None:
    """Create or update the research index from the shared index template."""
    schema = json.loads(INDEX_TEMPLATE.read_text(encoding="utf-8"))
    index = SearchIndex(schema)
    index.name = INDEX_NAME
    if openai_endpoint and index.vector_search and index.vector_search.vectorizers:
        vectorizer = index.vector_search.vectorizers[0]
        if isinstance(vectorizer, AzureOpenAIVectorizer) and vectorizer.parameters:
            vectorizer.parameters.resource_url = openai_endpoint
            vectorizer.parameters.deployment_name = deployment
    async with SearchIndexClient(endpoint=endpoint, credential=credential) as client:
        await client.create_or_update_index(index)
    print(f"Created index: {INDEX_NAME}")


async def upload(endpoint: str, credential: Any, docs: list[dict[str, Any]]) -> None:
    """Push documents in batches the service will accept."""
    async with SearchClient(
        endpoint=endpoint, index_name=INDEX_NAME, credential=credential
    ) as client:
        for start in range(0, len(docs), 100):
            await client.upload_documents(documents=docs[start : start + 100])
    print(f"Uploaded {len(docs)} documents")


async def create_knowledge_base(
    endpoint: str, credential: Any, openai_endpoint: str, chat_deployment: str
) -> None:
    """Create the knowledge source and the knowledge base that agents will call."""
    async with SearchIndexClient(endpoint=endpoint, credential=credential) as client:
        source = SearchIndexKnowledgeSource(
            name=INDEX_NAME,
            description=(
                "Retrieval-augmented generation research corpus: paper summaries, the "
                "methods, datasets, tasks and metrics each paper uses, and the "
                "limitations papers report about their own work."
            ),
            search_index_parameters=SearchIndexKnowledgeSourceParameters(
                search_index_name=INDEX_NAME,
                source_data_fields=[
                    SearchIndexFieldReference(name="uid"),
                    SearchIndexFieldReference(name="snippet"),
                    SearchIndexFieldReference(name="blob_path"),
                    SearchIndexFieldReference(name="snippet_parent_id"),
                ],
                search_fields=[SearchIndexFieldReference(name="snippet")],
                semantic_configuration_name="semantic-configuration",
            ),
        )
        await client.create_or_update_knowledge_source(knowledge_source=source)
        print(f"Created knowledge source: {INDEX_NAME}")

        models = []
        if openai_endpoint and chat_deployment:
            models = [
                KnowledgeBaseAzureOpenAIModel(
                    azure_open_ai_parameters=AzureOpenAIVectorizerParameters(
                        resource_url=openai_endpoint,
                        deployment_name=chat_deployment,
                        model_name=chat_deployment,
                    )
                )
            ]
        knowledge_base = KnowledgeBase(
            name=KB_NAME,
            description=(
                "Answers questions about the retrieval-augmented generation research "
                "literature: what a paper contributes, which methods and datasets it "
                "builds on, and what limitations the field reports."
            ),
            knowledge_sources=[KnowledgeSourceReference(name=INDEX_NAME)],
            retrieval_reasoning_effort=KnowledgeRetrievalLowReasoningEffort(),
            output_mode=KnowledgeRetrievalOutputMode.EXTRACTIVE_DATA,
            **(dict(models=models) if models else {}),
        )
        await client.create_or_update_knowledge_base(knowledge_base=knowledge_base)
        print(f"Created knowledge base: {KB_NAME}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the documents without calling Azure.",
    )
    return parser.parse_args()


async def main_async(dry_run: bool) -> int:
    docs = build_documents(load_graph())
    kinds: dict[str, int] = defaultdict(int)
    for doc in docs:
        kinds[doc["uid"].split("-", 1)[0]] += 1
    print(f"Built {len(docs)} documents: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))

    if dry_run:
        for doc in docs[:1]:
            print("\n--- sample ---\n" + doc["snippet"][:1200])
        return 0

    endpoint = os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"]
    openai_endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    embed_deployment = os.environ.get(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
    )
    chat_deployment = os.environ.get("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "")
    tenant_id = os.environ.get("AZURE_TENANT_ID")

    async with AzureDeveloperCliCredential(tenant_id=tenant_id) as credential:
        print("Embedding documents...")
        await embed(docs, openai_endpoint, embed_deployment, credential)
        await create_index(endpoint, credential, openai_endpoint, embed_deployment)
        await upload(endpoint, credential, docs)
        await create_knowledge_base(endpoint, credential, openai_endpoint, chat_deployment)

    print(f"\nKnowledge base '{KB_NAME}' is ready.")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
