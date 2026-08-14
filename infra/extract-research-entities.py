"""Extract a research knowledge graph from the ingested arXiv corpus.

The papers are already chunked and indexed in Azure AI Search by
`arxiv-papers-knowledge-source`. This script reads that text back, asks the Foundry
model to extract structured research entities from each paper, canonicalizes the
resulting vocabulary, and caches everything to JSON for the Fabric graph build.

Extracted per paper:
  * essence       - one-sentence statement of what the paper contributes
  * proposes      - methods, models, or systems the paper introduces
  * uses          - existing methods it explicitly builds on
  * tasks         - problems it addresses
  * datasets      - datasets or benchmarks it evaluates on
  * metrics       - evaluation metrics it reports
  * limitations   - limitations the authors state themselves

Entity names are normalized in a second pass so that "CoT", "chain of thought", and
"Chain-of-Thought prompting" collapse to a single node.
"""

import collections
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from azure.identity import AzureCliCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from dotenv import load_dotenv
from openai import AzureOpenAI

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
CACHE_PATH = REPO_ROOT / "data" / "research-graph" / "paper-entities.json"

load_dotenv(ENV_PATH, override=True)

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_SERVICE_ENDPOINT"]
INDEX_NAME = "arxiv-papers-knowledge-source-index"
MODEL_DEPLOYMENT = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")
TENANT_ID = os.environ["AZURE_TENANT_ID"]

# Papers are long; this keeps each extraction call well inside the context window
# while still covering abstract, method, and experiment sections.
MAX_PAPER_CHARS = 140_000
EXTRACTION_WORKERS = 5

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "essence",
        "proposes",
        "uses",
        "tasks",
        "datasets",
        "metrics",
        "limitations",
    ],
    "properties": {
        "essence": {
            "type": "string",
            "description": "One precise sentence stating what this paper contributes.",
        },
        "proposes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Named methods, models, architectures, or systems this paper "
                "introduces. Use the paper's own name for them."
            ),
        },
        "uses": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Named existing methods or components this paper explicitly builds "
                "on or incorporates, not merely mentions."
            ),
        },
        "tasks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Problems or task types the paper addresses.",
        },
        "datasets": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Datasets or benchmarks the paper actually evaluates on, not ones "
                "only cited in related work."
            ),
        },
        "metrics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evaluation metrics the paper reports.",
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Limitations or failure modes the authors state themselves.",
        },
    },
}

EXTRACTION_PROMPT = """You are building a research knowledge graph from a paper.

Extract only what the paper itself states. Do not infer, and do not include items
that appear only in the reference list or related-work discussion unless the paper
actually uses them.

Rules:
- Use canonical short names ("Chain-of-Thought", not "the chain of thought prompting
  approach described in section 3").
- `proposes` is for contributions of THIS paper. `uses` is for prior work it builds on.
- Prefer specific named entities over generic phrases. Skip vague items like
  "a neural network" or "our approach".
- Keep each list to at most 8 of the most important items.
- If a category genuinely does not apply, return an empty list.

Paper filename: {filename}

Paper text:
{text}"""

CANONICALIZE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical", "aliases"],
                "properties": {
                    "canonical": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

CANONICALIZE_PROMPT = """These are {kind} names extracted from 20 machine-learning papers.

Group names that refer to the SAME underlying {kind} and choose one canonical form.

Merge aggressively when the names denote the same thing:
- Abbreviations with expansions ("CoT" with "Chain-of-Thought prompting").
- Casing, punctuation, hyphenation, and pluralization variants.
- Model family variants that differ only by version, size, or serving suffix
  ("GPT-3.5" with "GPT-3.5-turbo"; "Llama 2" with "Llama-2-7B").
- A base method and a named checkpoint of that same method
  ("Contriever" with "Contriever-MS MARCO").
- Generic descriptive phrases that name an established concept
  ("dense retriever" with "dense retrieval").

Do NOT merge genuinely distinct entities that merely sound similar:
- "Natural Questions" and "NarrativeQA" are different datasets.
- "RAG" and "GraphRAG" and "RAG-Fusion" are different methods.
- "BM25" and "DPR" are different retrievers.

Prefer the most widely recognized short form as the canonical name.
Every input name must appear in exactly one group's aliases, including names that
form a group on their own.

Names:
{names}"""


def get_clients() -> tuple[SearchClient, AzureOpenAI]:
    """Build the Search and Azure OpenAI clients."""
    credential = AzureCliCredential(tenant_id=TENANT_ID)
    search_client = SearchClient(
        endpoint=SEARCH_ENDPOINT, index_name=INDEX_NAME, credential=credential
    )
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    openai_client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"].split("/api/projects")[0],
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
        timeout=600,
        max_retries=4,
    )
    return search_client, openai_client


def load_paper_texts(search_client: SearchClient) -> dict[str, str]:
    """Reassemble each paper's text from its indexed chunks."""
    chunks: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for document in search_client.search(
        search_text="*",
        select=["uid", "snippet", "metadata_storage_path"],
        top=100_000,
    ):
        uid = document["uid"]
        filename = document.get("metadata_storage_path") or uid.split("_")[0]
        order = int(uid.rsplit("_", 1)[-1]) if "_" in uid else 0
        chunks[filename].append((order, document.get("snippet") or ""))

    papers = {}
    for filename, parts in chunks.items():
        text = "\n".join(snippet for _, snippet in sorted(parts))
        papers[filename] = text[:MAX_PAPER_CHARS]
    return papers


def structured_call(
    openai_client: AzureOpenAI, prompt: str, schema: dict, schema_name: str
) -> dict:
    """Call the model and parse a schema-constrained JSON response."""
    response = openai_client.chat.completions.create(
        model=MODEL_DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        },
        max_completion_tokens=32_000,
    )
    return json.loads(response.choices[0].message.content)


def extract_paper(
    openai_client: AzureOpenAI, filename: str, text: str
) -> tuple[str, dict]:
    """Extract structured research entities from one paper."""
    prompt = EXTRACTION_PROMPT.format(filename=filename, text=text)
    for attempt in range(5):
        try:
            result = structured_call(
                openai_client, prompt, EXTRACTION_SCHEMA, "paper_entities"
            )
            print(
                f"  ok  {filename[:58]:58s} "
                f"proposes={len(result['proposes'])} datasets={len(result['datasets'])} "
                f"tasks={len(result['tasks'])}"
            )
            return filename, result
        except Exception as error:  # noqa: BLE001 - retried, then surfaced
            if attempt == 4:
                print(f"  FAIL {filename[:58]:58s} {str(error)[:90]}")
                return filename, {}
            # Token-per-minute limits recover on a minute boundary, so back off
            # well past a full window rather than retrying tightly.
            time.sleep(30 * (attempt + 1))
    return filename, {}


def canonicalize(
    openai_client: AzureOpenAI, names: list[str], kind: str
) -> dict[str, str]:
    """Map every raw entity name to a canonical name."""
    if not names:
        return {}
    prompt = CANONICALIZE_PROMPT.format(kind=kind, names="\n".join(sorted(names)))
    result = structured_call(
        openai_client, prompt, CANONICALIZE_SCHEMA, "canonical_groups"
    )

    mapping: dict[str, str] = {}
    for group in result["groups"]:
        canonical = group["canonical"].strip()
        for alias in group["aliases"]:
            mapping[alias.strip().lower()] = canonical

    # Any name the model omitted keeps its own cleaned form.
    for name in names:
        mapping.setdefault(name.strip().lower(), name.strip())

    distinct = len({v for v in mapping.values()})
    print(f"  {kind:9s} {len(names):4d} raw -> {distinct:4d} canonical")
    return mapping


THEME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["themes"],
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["theme", "statement_numbers"],
                "properties": {
                    "theme": {"type": "string"},
                    "statement_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
            },
        }
    },
}

THEME_PROMPT = """Below are limitations that the authors of 20 retrieval-augmented
generation papers reported about their own work.

Group them into 8 to 14 recurring themes that describe the shared weakness, so a
reader can see which problems the field keeps running into.

Rules:
- Name each theme as a short noun phrase describing the weakness itself,
  for example "Retrieval quality bounds end-task accuracy" or
  "Evaluation limited to English".
- Assign every statement number to exactly one theme.
- Do not invent statement numbers.

Statements:
{statements}"""


def arxiv_id_from_filename(filename: str) -> str:
    """Recover the base arXiv id from a downloaded filename."""
    match = re.match(r"(\d{4}\.\d{4,5})v?\d*_", filename)
    return match.group(1) if match else ""


def cluster_limitations(
    openai_client: AzureOpenAI, statements: list[str]
) -> dict[str, str]:
    """Assign each reported limitation to a recurring corpus-wide theme.

    Individually, a self-reported limitation is a footnote. Grouped across the
    corpus it becomes the field's open-problem list, which is the question
    researchers actually want the graph to answer.
    """
    if not statements:
        return {}
    numbered = "\n".join(
        f"{index}. {statement}" for index, statement in enumerate(statements, start=1)
    )
    result = structured_call(
        openai_client,
        THEME_PROMPT.format(statements=numbered),
        THEME_SCHEMA,
        "limitation_themes",
    )

    themes: dict[str, str] = {}
    for group in result["themes"]:
        label = " ".join(group["theme"].split())
        for number in group["statement_numbers"]:
            if 1 <= number <= len(statements):
                themes[statements[number - 1]] = label

    unthemed = [s for s in statements if s not in themes]
    for statement in unthemed:
        themes[statement] = "Other"
    print(
        f"  {len(statements)} limitations -> {len(set(themes.values()))} themes"
        + (f" ({len(unthemed)} unassigned)" if unthemed else "")
    )
    return themes


def main() -> None:
    """Extract, canonicalize, and cache the research entity graph."""
    search_client, openai_client = get_clients()

    # Extraction and canonicalization are both non-deterministic, so previous
    # results are reused. Re-running then only fills genuine gaps instead of
    # silently reshuffling the vocabulary that the Fabric graph is built from.
    cached = (
        json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if CACHE_PATH.exists()
        else {}
    )
    extracted: dict[str, dict] = dict(cached.get("raw", {}))

    print("Loading paper text from the search index...")
    papers = load_paper_texts(search_client)
    print(f"Reassembled {len(papers)} papers\n")

    pending = {
        filename: text for filename, text in papers.items() if filename not in extracted
    }
    if extracted:
        print(f"Reusing {len(extracted)} cached extractions")
    if pending:
        print(f"Extracting {len(pending)} papers with {MODEL_DEPLOYMENT}...")
        with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as pool:
            futures = [
                pool.submit(extract_paper, openai_client, filename, text)
                for filename, text in sorted(pending.items())
            ]
            for future in futures:
                filename, result = future.result()
                if result:
                    extracted[filename] = result

    print(f"\nExtracted {len(extracted)} of {len(papers)} papers\n")
    if len(extracted) != len(papers):
        missing = sorted(set(papers) - set(extracted))
        raise RuntimeError(
            "Extraction incomplete; re-run after checking model quota. "
            f"Missing: {', '.join(name[:40] for name in missing)}"
        )

    print("Canonicalizing entity vocabulary...")
    buckets = {
        "method": ["proposes", "uses"],
        "task": ["tasks"],
        "dataset": ["datasets"],
        "metric": ["metrics"],
    }
    cached_vocabulary = cached.get("vocabulary", {})
    mappings: dict[str, dict[str, str]] = {}
    for kind, fields in buckets.items():
        names = sorted(
            {
                value.strip()
                for record in extracted.values()
                for field in fields
                for value in record.get(field, [])
                if value.strip()
            }
        )
        known = cached_vocabulary.get(kind, {})
        if names and all(name.lower() in known for name in names):
            print(f"  {kind:9s} {len(names):4d} raw -> reusing cached vocabulary")
            mappings[kind] = known
        else:
            mappings[kind] = canonicalize(openai_client, names, kind)

    def canonical(kind: str, values: list[str]) -> list[str]:
        """Apply a vocabulary mapping, preserving order and dropping duplicates."""
        mapped = [
            mappings[kind].get(value.strip().lower(), value.strip())
            for value in values
            if value.strip()
        ]
        return list(dict.fromkeys(mapped))

    print("Grouping reported limitations into recurring themes...")
    statements = sorted(
        {
            " ".join(text.split())
            for record in extracted.values()
            for text in record.get("limitations", [])
            if text.strip()
        }
    )
    cached_themes = cached.get("limitation_themes", {})
    if statements and all(statement in cached_themes for statement in statements):
        print(f"  reusing {len(cached_themes)} cached themes")
        themes = cached_themes
    else:
        themes = cluster_limitations(openai_client, statements)

    payload = {
        "papers": {
            filename: {
                "arxiv_id": arxiv_id_from_filename(filename),
                "essence": record.get("essence", ""),
                "proposes": canonical("method", record.get("proposes", [])),
                "uses": canonical("method", record.get("uses", [])),
                "tasks": canonical("task", record.get("tasks", [])),
                "datasets": canonical("dataset", record.get("datasets", [])),
                "metrics": canonical("metric", record.get("metrics", [])),
                "limitations": [
                    {
                        "statement": " ".join(text.split()),
                        "theme": themes.get(" ".join(text.split()), "Other"),
                    }
                    for text in record.get("limitations", [])
                    if text.strip()
                ],
            }
            for filename, record in sorted(extracted.items())
        },
        "vocabulary": mappings,
        "limitation_themes": themes,
        "raw": extracted,
    }

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    totals = collections.Counter()
    for record in payload["papers"].values():
        for field in ("proposes", "uses", "tasks", "datasets", "metrics"):
            totals[field] += len(record[field])
    distinct = {
        field: len({v for r in payload["papers"].values() for v in r[field]})
        for field in ("proposes", "uses", "tasks", "datasets", "metrics")
    }

    print("\n--- extraction summary ---")
    for field in ("proposes", "uses", "tasks", "datasets", "metrics"):
        print(f"  {field:10s} {totals[field]:4d} mentions, {distinct[field]:3d} distinct")
    print(f"  limitations {len(statements):3d} statements, {len(set(themes.values()))} themes")
    for theme, count in collections.Counter(themes.values()).most_common():
        print(f"    {count:3d}  {theme}")
    print(f"\nCached to {CACHE_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
