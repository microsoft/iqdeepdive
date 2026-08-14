"""Compute a VOSviewer-style science map from the exported research graph.

The map answers a different question from the node-link graph. Instead of "what is
connected to what", it shows where this literature is crowded and where it is thin:
papers that cite the same work, and are cited together, end up near each other, so
clusters are subfields and empty space is unexplored ground.

Method, in order:

1.  Similarity. Each paper becomes a sparse vector over the papers it cites
    (bibliographic coupling) and the papers that cite it (co-citation). Cosine
    similarity between those vectors is the standard bibliometric measure of "these
    two papers are about the same thing", and unlike keyword overlap it needs no
    full text, so it covers all 119 papers rather than only the 20 indexed ones.
2.  Layout. Dissimilarity is 1 - cosine, projected to 2D by SMACOF stress
    majorization. This is the same family of technique VOSviewer uses; distance on
    the map approximates dissimilarity.
3.  Clusters. Louvain community detection over the similarity graph.
4.  Cluster labels. Taken from the methods, tasks and datasets the LLM extracted
    from the papers in each cluster, scored so that a term shared by the whole
    corpus loses to one that is distinctive to the cluster.

Reads  web/research-explorer/graph.json  (written by infra/export-research-graph.py)
Writes web/research-explorer/map.json

Usage:
  uv run python infra/export-research-graph.py
  uv run python infra/build-research-map.py
"""

import collections
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

import networkx as nx
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = REPO_ROOT / "web" / "research-explorer" / "graph.json"
OUTPUT_PATH = REPO_ROOT / "web" / "research-explorer" / "map.json"

SEED = 7
SMACOF_ITERATIONS = 400
SMACOF_TOLERANCE = 1e-7
# Two papers can land on top of each other during majorization. Pairs closer than this
# are treated as contributing nothing to the update rather than dividing by zero.
SMACOF_MIN_DISTANCE = 1e-9
# Above 1.0 Louvain splits more finely; the default gives 3 very coarse blobs here.
LOUVAIN_RESOLUTION = 1.6
LABEL_TERM_TYPES = ("Method", "Task", "Dataset")

# Only the 20 indexed papers have extracted entities, so cluster labels are built from
# title phrases, which every paper has. Extracted methods are reported alongside as a
# richer second signal wherever they exist.
STOPWORDS = frozenset("""
a an and are as at be by for from how in into is it its of on or over that the their
this to via with without we our using use used toward towards can does do not new
approach approaches method methods framework frameworks study studies analysis survey
paper papers work works based case cases general generic simple better improving
improve improved efficient effective scalable towards understanding evaluation
evaluating learning model models language large
""".split())
SEMANTIC_EDGES = ("PROPOSES", "USES", "ADDRESSES", "EVALUATES_ON", "MEASURES")

# Distinct hues, ordered so the largest clusters get the most separable colours.
CLUSTER_COLORS = [
    "#e63946", "#4cc9f0", "#f4a261", "#52b788", "#b07bac",
    "#ffd166", "#5c7cfa", "#f72585", "#2a9d8f", "#adb5bd",
]


def cosine_similarity(vectors: dict[str, set[str]], ids: list[str]) -> np.ndarray:
    """Cosine similarity between sparse binary vectors, as a dense matrix."""
    index = {node_id: i for i, node_id in enumerate(ids)}
    norms = np.array([math.sqrt(len(vectors[i])) or 1.0 for i in ids])

    # Invert to a term -> papers posting list so only pairs that actually share a
    # term are visited; the full pairwise loop would touch 7,000 empty pairs.
    postings: dict[str, list[int]] = collections.defaultdict(list)
    for node_id in ids:
        for term in vectors[node_id]:
            postings[term].append(index[node_id])

    overlap = np.zeros((len(ids), len(ids)))
    for holders in postings.values():
        for a in range(len(holders)):
            for b in range(a + 1, len(holders)):
                overlap[holders[a], holders[b]] += 1
                overlap[holders[b], holders[a]] += 1

    sim = overlap / np.outer(norms, norms)
    np.fill_diagonal(sim, 0.0)
    return sim


def smacof(dissimilarity: np.ndarray, weights: np.ndarray, rng) -> np.ndarray:
    """Project a dissimilarity matrix to 2D by SMACOF stress majorization.

    Plain metric MDS rather than a force simulation, so the same input always gives
    the same map and distance on screen means something.
    """
    n = len(dissimilarity)
    x = rng.normal(scale=0.4, size=(n, 2))

    v = -weights.copy()
    np.fill_diagonal(v, 0.0)
    np.fill_diagonal(v, -v.sum(axis=1))
    v_inv = np.linalg.pinv(v)

    previous = None
    for _ in range(SMACOF_ITERATIONS):
        diff = x[:, None, :] - x[None, :, :]
        distance = np.sqrt((diff**2).sum(axis=-1))
        np.fill_diagonal(distance, 1.0)

        b = -weights * dissimilarity / np.where(
            distance > SMACOF_MIN_DISTANCE, distance, np.inf
        )
        np.fill_diagonal(b, 0.0)
        np.fill_diagonal(b, -b.sum(axis=1))
        x = v_inv @ b @ x

        stress = float((weights * (distance - dissimilarity) ** 2).sum() / 2)
        if previous is not None and abs(previous - stress) < SMACOF_TOLERANCE:
            break
        previous = stress

    x -= x.mean(axis=0)
    # Orient the map deterministically: longest spread on the horizontal axis, which
    # suits a wide screen and keeps re-runs from silently rotating the picture.
    _, _, rotation = np.linalg.svd(x, full_matrices=False)
    x = x @ rotation.T
    x /= np.abs(x).max() or 1.0
    return x


def title_phrases(title: str) -> set[str]:
    """Unigrams and bigrams from a title, minus generic research vocabulary."""
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-+]*", title.lower()) if len(w) > 2]
    keep = [w for w in words if w not in STOPWORDS]
    grams = set(keep)
    for a, b in zip(words, words[1:], strict=False):
        if a not in STOPWORDS and b not in STOPWORDS:
            grams.add(f"{a} {b}")
    return grams


def overlapping(a: str, b: str) -> bool:
    """True when one phrase contains the other, in either direction."""
    a, b = a.lower(), b.lower()
    return a in b or b in a


def label_clusters(members, paper_terms, term_name, total_papers) -> dict[int, str]:
    """Name each cluster after the terms most distinctive to its papers."""
    document_frequency = collections.Counter()
    for terms in paper_terms.values():
        for term in terms:
            document_frequency[term] += 1

    labels = {}
    for cluster_id, paper_ids in members.items():
        cluster_frequency = collections.Counter()
        for paper_id in paper_ids:
            for term in paper_terms.get(paper_id, ()):
                cluster_frequency[term] += 1

        # A term mentioned by a single paper describes that paper, not the cluster,
        # so it is only used when nothing recurs.
        recurring = {t: c for t, c in cluster_frequency.items() if c >= 2}
        candidates = recurring or cluster_frequency
        scores = {
            term: count * math.log(1 + total_papers / document_frequency[term])
            for term, count in candidates.items()
        }
        ranked = sorted(scores, key=lambda t: (-scores[t], term_name[t]))
        # "retrieval" adds nothing next to "passage retrieval", so a shorter phrase
        # already contained in a chosen one is skipped.
        chosen: list[str] = []
        for term in ranked:
            name = term_name[term]
            if any(overlapping(name, term_name[c]) for c in chosen):
                continue
            chosen.append(term)
            if len(chosen) == 3:
                break
        labels[cluster_id] = " · ".join(term_name[t] for t in chosen)
    return labels


def main() -> None:
    if not GRAPH_PATH.exists():
        raise SystemExit(
            f"{GRAPH_PATH.relative_to(REPO_ROOT)} not found - "
            "run infra/export-research-graph.py first."
        )
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    by_id = {node["id"]: node for node in graph["nodes"]}
    papers = [n for n in graph["nodes"] if n["type"] == "Paper"]
    ids = [n["id"] for n in papers]
    print(f"Mapping {len(ids)} papers")

    # Citation context: what a paper cites, and what cites it. Two papers pointing at
    # the same literature, or being reached for by the same later work, are treated
    # as neighbours - this is what makes the map cover papers whose text we never saw.
    context: dict[str, set[str]] = {i: set() for i in ids}
    for link in graph["links"]:
        if link["type"] != "CITES":
            continue
        context[link["source"]].add("out:" + link["target"])
        context[link["target"]].add("in:" + link["source"])

    sim = cosine_similarity(context, ids)
    pairs = int((sim > 0).sum() // 2)
    print(f"  {pairs} similar pairs (of {len(ids) * (len(ids) - 1) // 2} possible)")

    # Every pair gets a small weight so unrelated papers still push apart; similar
    # pairs dominate. Without the floor the layout tears into drifting islands.
    dissimilarity = 1.0 - sim / (sim.max() or 1.0)
    np.fill_diagonal(dissimilarity, 0.0)
    weights = sim / (sim.max() or 1.0) + 0.02
    np.fill_diagonal(weights, 0.0)

    coords = smacof(dissimilarity, weights, np.random.default_rng(SEED))

    similarity_graph = nx.Graph()
    similarity_graph.add_nodes_from(ids)
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            if sim[a, b] > 0:
                similarity_graph.add_edge(ids[a], ids[b], weight=float(sim[a, b]))
    communities = nx.community.louvain_communities(
        similarity_graph, weight="weight", seed=SEED, resolution=LOUVAIN_RESOLUTION
    )
    communities = sorted(communities, key=len, reverse=True)
    cluster_of = {
        node_id: i for i, group in enumerate(communities) for node_id in group
    }
    print(f"  {len(communities)} clusters: {[len(c) for c in communities]}")

    paper_terms: dict[str, set[str]] = collections.defaultdict(set)
    for link in graph["links"]:
        if link["type"] in SEMANTIC_EDGES:
            target = by_id[link["target"]]
            if target["type"] in LABEL_TERM_TYPES:
                paper_terms[link["source"]].add(target["id"])
    term_name = {n["id"]: n["name"] for n in graph["nodes"]}
    members = {i: list(group) for i, group in enumerate(communities)}
    phrases = {node_id: title_phrases(by_id[node_id]["name"]) for node_id in ids}
    labels = label_clusters(
        members, phrases, {p: p for p in {g for s in phrases.values() for g in s}}, len(ids)
    )
    method_labels = label_clusters(members, paper_terms, term_name, len(ids))

    items = []
    for (node_id, (x, y)) in zip(ids, coords, strict=True):
        node = by_id[node_id]
        items.append({
            "id": node_id,
            "x": round(float(x), 4),
            "y": round(float(y), 4),
            # Map weight is world citations, so a hotspot means the field cares,
            # not merely that our corpus happens to link it a lot.
            "weight": int(node.get("citationCount") or 0),
            "cluster": cluster_of[node_id],
            "title": node["name"],
            "tier": node.get("tier"),
            "year": node.get("year"),
            "venue": node.get("venue"),
            "arxivId": node.get("arxivId"),
            "essence": node.get("essence"),
            "terms": sorted(term_name[t] for t in paper_terms.get(node_id, ())),
        })

    clusters = [
        {
            "id": i,
            "label": labels[i],
            "methods": method_labels[i],
            "color": CLUSTER_COLORS[i % len(CLUSTER_COLORS)],
            "size": len(group),
            "papers": sum(1 for n in group if by_id[n].get("tier") == "core"),
        }
        for i, group in enumerate(communities)
    ]

    payload = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "bibliographic coupling + co-citation, cosine similarity, SMACOF",
        "graphId": graph.get("graphId"),
        "items": items,
        "clusters": clusters,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    for cluster in clusters:
        print(f"  [{cluster['size']:>3}] {cluster['label']}")
        print(f"        methods: {cluster['methods']}")


if __name__ == "__main__":
    main()
