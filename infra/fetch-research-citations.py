"""Fetch the citation neighbourhood of the research-literature corpus.

The corpus is defined in ``data/research-corpus.json``: list the arXiv ids you care
about there and every downstream step -- the Fabric graph, the ontology, and the Foundry
IQ knowledge base -- follows, because each one derives its scope from the cache this
script writes. Nothing below this file is specific to the papers that ship with it.

Uses the keyless Semantic Scholar Graph API. The result is cached to JSON so the
Fabric build steps stay deterministic and can be re-run without hitting the API again.

The cached graph has three tiers of papers:

- ``core``       the papers listed in the corpus definition
- ``foundation`` external papers that several core papers cite, i.e. the shared
                 intellectual ancestry of the corpus
- ``descendant`` external papers that cite several core papers, i.e. later work
                 that synthesizes the corpus

The two thresholds that decide how far the outer tiers reach are corpus settings too.

Citation edges carry Semantic Scholar's ``intents`` (background / methodology /
result) and ``isInfluential`` flag, which is what makes citation questions
answerable rather than merely countable.

Usage:
    uv run python infra/fetch-research-citations.py
    uv run python infra/fetch-research-citations.py --corpus data/my-corpus.json
"""

import argparse
import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).parents[1]
CORPUS_PATH = REPO_ROOT / "data" / "research-corpus.json"
CACHE_PATH = REPO_ROOT / "data" / "research-graph" / "citation-graph.json"
# Fetching every reference and citation takes minutes and burns anonymous quota,
# so keep the raw responses and re-derive the graph from them on later runs.
RAW_CACHE_PATH = REPO_ROOT / "data" / "research-graph" / "raw-s2-edges.json"

S2_API = "https://api.semanticscholar.org/graph/v1"

# Anonymous callers get a small shared quota; pause between every call.
REQUEST_PAUSE_SECONDS = 3.0

# Author lists on multi-lab papers explode into O(n^2) co-authorship edges.
MAX_AUTHORS_FOR_COAUTHORSHIP = 12


class Corpus(NamedTuple):
    """The papers to build the graph around, and how far out to reach from them."""

    arxiv_ids: list[str]
    foundation_min_core_citers: int
    descendant_min_core_cited: int


def load_corpus(path: Path) -> Corpus:
    """Read the corpus definition.

    The paper list lives in data/research-corpus.json rather than in this file so the
    sample can be pointed at a different literature without editing any code. Every
    downstream step derives its scope from the cache this script writes, so changing
    that one file changes the whole graph, knowledge base, and ontology.
    """
    if not path.exists():
        raise SystemExit(f"Corpus definition not found: {path}")
    corpus = json.loads(path.read_text(encoding="utf-8"))
    papers = corpus.get("papers") or []
    arxiv_ids = [str(paper["arxivId"]).strip() for paper in papers if paper.get("arxivId")]
    if not arxiv_ids:
        raise SystemExit(f"{path} lists no papers with an arxivId")
    duplicates = {value for value in arxiv_ids if arxiv_ids.count(value) > 1}
    if duplicates:
        raise SystemExit(f"{path} repeats arXiv ids: {', '.join(sorted(duplicates))}")
    thresholds = corpus.get("thresholds") or {}
    return Corpus(
        arxiv_ids=arxiv_ids,
        foundation_min_core_citers=int(thresholds.get("foundationMinCoreCiters", 4)),
        descendant_min_core_cited=int(thresholds.get("descendantMinCoreCited", 6)),
    )


PAPER_FIELDS = "paperId,title,year,citationCount,externalIds,authors,venue,publicationVenue"
EDGE_FIELDS = f"intents,isInfluential,{PAPER_FIELDS}"


def request_json(url: str, payload: dict | None = None, attempts: int = 6):
    """GET or POST JSON with backoff. Semantic Scholar throttles anonymous callers."""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8") if payload else None,
                headers={"Content-Type": "application/json"} if payload else {},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Exhausted retries for {url}")


def paged(path: str, fields: str, limit: int = 1000) -> list[dict]:
    """Walk an offset-paged Semantic Scholar edge collection."""
    rows: list[dict] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {"fields": fields, "limit": min(limit, 1000), "offset": offset}
        )
        page = request_json(f"{S2_API}/{path}?{query}")
        data = page.get("data", [])
        rows.extend(data)
        time.sleep(REQUEST_PAUSE_SECONDS)
        if "next" not in page or not data or len(rows) >= limit:
            return rows[:limit]
        offset = page["next"]


def clean(text: str | None) -> str:
    """Collapse whitespace so CSV round-trips keep one row per record."""
    return " ".join((text or "").split())


def paper_record(raw: dict, tier: str) -> dict:
    """Flatten a Semantic Scholar paper into the shape the lakehouse loads."""
    venue = clean(raw.get("venue")) or clean(
        (raw.get("publicationVenue") or {}).get("name")
    )
    return {
        "paper_id": raw["paperId"],
        "arxiv_id": (raw.get("externalIds") or {}).get("ArXiv", ""),
        "title": clean(raw.get("title")),
        "year": raw.get("year") or 0,
        "citation_count": raw.get("citationCount") or 0,
        "venue": venue or "Unpublished / preprint",
        "tier": tier,
    }


def fetch_core(arxiv_ids: list[str]) -> list[dict]:
    """Resolve every arXiv id to its Semantic Scholar record."""
    papers = request_json(
        f"{S2_API}/paper/batch?fields={PAPER_FIELDS}",
        {"ids": [f"ARXIV:{arxiv_id}" for arxiv_id in arxiv_ids]},
    )
    resolved = [paper for paper in papers if paper]
    if len(resolved) != len(arxiv_ids):
        raise RuntimeError(f"Resolved only {len(resolved)} of {len(arxiv_ids)} papers")
    return resolved


def fetch_edges(core: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return every outbound reference and inbound citation of the core papers."""
    references: list[dict] = []
    citations: list[dict] = []
    for index, paper in enumerate(core, start=1):
        outbound = paged(f"paper/{paper['paperId']}/references", EDGE_FIELDS)
        inbound = paged(f"paper/{paper['paperId']}/citations", EDGE_FIELDS, limit=1000)
        for row in outbound:
            if row.get("citedPaper", {}).get("paperId"):
                references.append({"src": paper["paperId"], **row})
        for row in inbound:
            if row.get("citingPaper", {}).get("paperId"):
                citations.append({"dst": paper["paperId"], **row})
        print(
            f"  [{index:2d}/{len(core)}] {clean(paper['title'])[:46]:46s} "
            f"refs={len(outbound):4d} cited_by={len(inbound):4d}"
        )
    return references, citations


def select_foundations(
    references: list[dict], core_ids: set[str], min_core_citers: int
) -> dict[str, dict]:
    """External papers that several core papers cite are the corpus's ancestry."""
    citers: dict[str, set[str]] = defaultdict(set)
    records: dict[str, dict] = {}
    for row in references:
        cited = row["citedPaper"]
        if cited["paperId"] in core_ids:
            continue
        citers[cited["paperId"]].add(row["src"])
        records[cited["paperId"]] = cited
    return {
        paper_id: records[paper_id]
        for paper_id, sources in citers.items()
        if len(sources) >= min_core_citers
    }


def select_descendants(
    citations: list[dict], core_ids: set[str], min_core_cited: int
) -> dict[str, dict]:
    """External papers that cite several core papers show later synthesis."""
    cited: dict[str, set[str]] = defaultdict(set)
    records: dict[str, dict] = {}
    for row in citations:
        citing = row["citingPaper"]
        if citing["paperId"] in core_ids:
            continue
        cited[citing["paperId"]].add(row["dst"])
        records[citing["paperId"]] = citing
    return {
        paper_id: records[paper_id]
        for paper_id, targets in cited.items()
        if len(targets) >= min_core_cited
    }


def citation_edge(src: str, dst: str, row: dict) -> dict:
    """Normalize one citation edge, keeping the properties that make it queryable."""
    intents = sorted(row.get("intents") or [])
    return {
        "src_paper_id": src,
        "dst_paper_id": dst,
        # Fabric Graph properties are scalars, so keep the list as a stable string.
        "intent": "|".join(intents) if intents else "uncategorized",
        "is_methodological": "methodology" in intents,
        "is_influential": bool(row.get("isInfluential")),
    }


def load_edges(core: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return every outbound reference and inbound citation, using the raw cache."""
    if RAW_CACHE_PATH.exists():
        raw = json.loads(RAW_CACHE_PATH.read_text(encoding="utf-8"))
        print(
            f"Reusing raw cache: {len(raw['references'])} references, "
            f"{len(raw['citations'])} citations "
            f"(delete {RAW_CACHE_PATH.name} to refetch)"
        )
        return raw["references"], raw["citations"]

    print("Fetching references and citations (this takes a few minutes)...")
    references, citations = fetch_edges(core)
    RAW_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_CACHE_PATH.write_text(
        json.dumps({"references": references, "citations": citations}),
        encoding="utf-8",
    )
    return references, citations


def main() -> None:
    """Fetch the corpus citation neighbourhood and cache it to disk."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=CORPUS_PATH,
        help="Corpus definition to build the graph from (default: data/research-corpus.json)",
    )
    args = parser.parse_args()

    corpus = load_corpus(args.corpus)
    print(f"Corpus: {len(corpus.arxiv_ids)} papers from {args.corpus.name}")

    print("Resolving core papers via Semantic Scholar...")
    core = fetch_core(corpus.arxiv_ids)
    core_ids = {paper["paperId"] for paper in core}
    print(f"Resolved {len(core)} core papers\n")

    references, citations = load_edges(core)

    foundations = select_foundations(
        references, core_ids, corpus.foundation_min_core_citers
    )
    descendants = select_descendants(
        citations, core_ids, corpus.descendant_min_core_cited
    )
    # A paper can be both an ancestor and a descendant of the corpus; ancestry wins.
    descendants = {
        paper_id: raw
        for paper_id, raw in descendants.items()
        if paper_id not in foundations
    }
    print(
        f"\n{len(foundations)} foundation papers "
        f"(cited by >= {corpus.foundation_min_core_citers} core), "
        f"{len(descendants)} descendant papers "
        f"(citing >= {corpus.descendant_min_core_cited} core)"
    )

    kept_ids = core_ids | set(foundations) | set(descendants)
    papers = (
        [paper_record(raw, "core") for raw in core]
        + [paper_record(raw, "foundation") for raw in foundations.values()]
        + [paper_record(raw, "descendant") for raw in descendants.values()]
    )

    edges: dict[tuple[str, str], dict] = {}
    for row in references:
        cited_id = row["citedPaper"]["paperId"]
        if cited_id in kept_ids:
            edges[(row["src"], cited_id)] = citation_edge(row["src"], cited_id, row)
    for row in citations:
        citing_id = row["citingPaper"]["paperId"]
        if citing_id in kept_ids:
            edges.setdefault(
                (citing_id, row["dst"]), citation_edge(citing_id, row["dst"], row)
            )

    authors: dict[str, str] = {}
    authorship: list[dict] = []
    coauthorship: Counter[tuple[str, str]] = Counter()
    raw_by_id = {
        **{paper["paperId"]: paper for paper in core},
        **foundations,
        **descendants,
    }
    for paper_id, raw in raw_by_id.items():
        named = [
            author
            for author in raw.get("authors") or []
            if author.get("authorId") and author.get("name")
        ]
        for author in named:
            authors[author["authorId"]] = clean(author["name"])
            authorship.append({"author_id": author["authorId"], "paper_id": paper_id})
        if len(named) <= MAX_AUTHORS_FOR_COAUTHORSHIP:
            for left, right in itertools.combinations(
                sorted(author["authorId"] for author in named), 2
            ):
                coauthorship[(left, right)] += 1

    venues = sorted({paper["venue"] for paper in papers})

    payload = {
        "papers": papers,
        "authors": [
            {"author_id": author_id, "name": name}
            for author_id, name in sorted(authors.items())
        ],
        "venues": [{"venue_id": venue, "name": venue} for venue in venues],
        "authorship": authorship,
        "coauthorship": [
            {"left_author_id": left, "right_author_id": right, "papers_together": count}
            for (left, right), count in sorted(coauthorship.items())
        ],
        "publication": [
            {"paper_id": paper["paper_id"], "venue_id": paper["venue"]}
            for paper in papers
        ],
        "citations": sorted(edges.values(), key=lambda edge: edge["src_paper_id"]),
    }

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    intent_counts = Counter(edge["intent"] for edge in payload["citations"])
    print("\n--- citation graph ---")
    print(f"  papers        {len(payload['papers']):5d}")
    print(f"  authors       {len(payload['authors']):5d}")
    print(f"  venues        {len(payload['venues']):5d}")
    print(f"  citations     {len(payload['citations']):5d}")
    print(f"  authorship    {len(payload['authorship']):5d}")
    print(f"  coauthorship  {len(payload['coauthorship']):5d}")
    for intent, count in intent_counts.most_common():
        print(f"    intent {intent:28s} {count:5d}")
    print(f"\nCached to {CACHE_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
