"""Create the research agent that pairs Foundry IQ with Fabric IQ.

Two retrieval shapes over one corpus, exposed as two MCP tools:

  knowledge_base_retrieve  Foundry IQ. Semantic retrieval over the paper cards, for
                           questions about meaning: what a paper argues, what it
                           reports, what it admits it cannot do.
  search_ontology          Fabric IQ. Typed traversal over papers, authors, venues,
                           methods, tasks, datasets, metrics, limitations and themes,
                           for questions about structure: what connects to what.

Fabric IQ is delegated-only, so the ontology tool is bound to an OAuth2 connection and
every ontology query runs as the signed-in user under that user's Fabric permissions.
The first call returns a consent link; afterwards the agent answers normally.

Prerequisites:
  infra/create-research-knowledge-py        creates research-literature-kb
  infra/create-research-ontology.py              creates ResearchLiteratureOntology
  infra/create-fabriciq-ontology-connection.py   creates the OAuth2 connection

Usage:
  uv run python infra/create-research-ontology-agent.py --grant-fabric-role
"""

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"

API_VERSION = "2025-11-15-preview"
AGENT_NAME = "agent-research-ontology"
KB_CONNECTION = "research-kb-mcp-connection"
ONTOLOGY_CONNECTION = os.getenv(
    "FABRIC_ONTOLOGY_CONNECTION_NAME", "fabriciq-ontology-connection"
)

INSTRUCTIONS = """You are a research intelligence analyst for a curated corpus of \
retrieval-augmented generation and language-model-agent literature. The corpus holds 20 \
core papers with fully extracted entities plus roughly 100 papers they cite, along with \
their authors, venues, methods, tasks, datasets, metrics and reported limitations.

You have two tools over the same corpus. Choose deliberately.

Use `knowledge_base_retrieve` (semantic retrieval over paper text) for questions about \
meaning: what a paper contributes, how an approach works, what problem it addresses, what \
limitations papers report, how two lines of work relate, what a theme covers. Prefer it \
whenever the answer is prose that must be grounded in what a paper actually says.

Use `search_ontology` (typed traversal over the research entities) for questions about \
structure and counts: which paper is cited most, how many papers use a dataset, which \
authors appear most often, how many limitations fall under a theme, which methods a venue's \
papers propose. Prefer it whenever the answer is a number, a ranking, a set produced by \
counting, or a path between two kinds of thing.

The ontology knows these entities and can traverse between them:
  Paper, Author, Venue, Method, Task, Dataset, Metric, Limitation, Theme
related by authored, coauthored, cites, publishedIn, proposes, uses, addresses, \
evaluatesOn, measures, reportsLimitation and hasTheme.

Some questions need both. "Which paper is most cited and why does the field build on it" \
is an ontology question followed by a knowledge base question. Run them in that order and \
say which part came from where.

Rules:
- Citation counts from the ontology are within this corpus, not global citation counts. \
Always say so when you report them.
- The ontology matches stored names literally. Venues are stored in full, so "Neural \
Information Processing Systems", not "NeurIPS", and themes are stored as full phrases. If \
a lookup returns nothing, try the full stored form once before concluding the corpus does \
not cover it.
- Never invent a paper, author, dataset or number. If a tool returns nothing, say the \
corpus does not cover it rather than answering from general knowledge.
- When the knowledge base grounds an answer, name the specific papers behind each claim.
- Be concise. Lead with the answer, then the evidence."""

DESCRIPTION = (
    "Research intelligence over the RAG literature: Foundry IQ for meaning, "
    "the Fabric ontology for structure."
)


def connection_scope() -> tuple[str, str]:
    """Return the ARM connections URL prefix and the Foundry account/project pair.

    The account and project names are parsed from FOUNDRY_PROJECT_ENDPOINT, which has the
    form https://{account}.services.ai.azure.com/api/projects/{project}, so no extra
    environment variables are needed.
    """
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    account = urlparse(endpoint).netloc.split(".", 1)[0]
    project = endpoint.rsplit("/", 1)[-1]
    subscription = os.environ["AZURE_SUBSCRIPTION_ID"].strip().strip("'")
    resource_group = os.environ["AZURE_RESOURCE_GROUP"].strip().strip("'")
    prefix = (
        f"https://management.azure.com/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}/providers/Microsoft.CognitiveServices"
        f"/accounts/{account}/projects/{project}/connections"
    )
    return prefix, f"{account}/{project}"


def ensure_connection(
    credential: AzureDeveloperCliCredential, name: str, target: str, audience: str
) -> None:
    """Create or update a RemoteTool project connection that authenticates as the project.

    The knowledge base connection is also declared in infra/core/ai/ai-project.bicep. It is
    written here as well so this one command reproduces the whole agent wiring.
    """
    prefix, scope = connection_scope()
    token = credential.get_token("https://management.azure.com/.default").token
    response = httpx.put(
        f"{prefix}/{name}",
        params={"api-version": "2025-04-01-preview"},
        headers={"Authorization": "Bearer " + token},
        json={
            "properties": {
                "category": "RemoteTool",
                "target": target,
                "authType": "ProjectManagedIdentity",
                "audience": audience,
                "isSharedToAll": True,
            }
        },
        timeout=120,
    )
    response.raise_for_status()
    print(f"Connection '{name}' ready on {scope}.")


def grant_fabric_workspace_role(credential: AzureDeveloperCliCredential) -> None:
    """Give the Foundry project identity a Fabric workspace role.

    Fabric authorizes against workspace membership, not Azure RBAC, so an Azure role
    assignment is not enough. This is idempotent: Fabric returns 400 once the principal
    already holds a role.
    """
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"].strip().strip("'")
    principal_id = os.environ["FOUNDRY_PROJECT_PRINCIPAL_ID"].strip().strip("'")
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token
    response = httpx.post(
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/roleAssignments",
        headers={"Authorization": "Bearer " + token},
        json={
            "principal": {"id": principal_id, "type": "ServicePrincipal"},
            "role": "Member",
        },
        timeout=60,
    )
    if response.status_code in (200, 201):
        print(f"Granted the project identity Member on Fabric workspace {workspace_id}.")
    else:
        print(
            f"Fabric role assignment returned {response.status_code}: "
            f"{response.text[:200]}"
        )


def knowledge_base_mcp_url(knowledge_base_name: str) -> str:
    """Build the Azure AI Search knowledge base MCP endpoint."""
    search_endpoint = os.environ["AZURE_AI_SEARCH_SERVICE_ENDPOINT"].rstrip("/")
    return (
        f"{search_endpoint}/knowledgebases/{knowledge_base_name}"
        "/mcp?api-version=2026-05-01-preview"
    )


def parse_args() -> argparse.Namespace:
    """Parse overrides for the agent, its model and its two connections."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=AGENT_NAME)
    parser.add_argument("--knowledge-base-name", default="research-literature-kb")
    parser.add_argument("--kb-connection-name", default=KB_CONNECTION)
    parser.add_argument("--ontology-connection-name", default=ONTOLOGY_CONNECTION)
    parser.add_argument(
        "--model", default=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")
    )
    parser.add_argument(
        "--skip-connections",
        action="store_true",
        help="Reuse the existing project connections instead of writing them.",
    )
    parser.add_argument(
        "--grant-fabric-role",
        action="store_true",
        help=(
            "Add the Foundry project's managed identity to the Fabric workspace as a "
            "Member. Required once before any Fabric tool can mint a workspace token."
        ),
    )
    return parser.parse_args()


def ontology_mcp_url() -> str:
    """Read the ontology's own MCP endpoint.

    Fabric IQ authenticates On-Behalf-Of the signed-in user and does not support
    application-only authentication, so this endpoint is paired with the OAuth2 connection
    written by `infra/create-fabriciq-ontology-connection.py` rather than with the project's
    managed identity. Fronting the ontology with a data agent does not avoid that
    requirement, because the data agent reads the ontology as whoever called it.
    """
    url = os.getenv("FABRIC_RESEARCH_ONTOLOGY_MCP_URL", "").strip().strip("'")
    if not url:
        raise RuntimeError(
            "FABRIC_RESEARCH_ONTOLOGY_MCP_URL is not set. "
            "Run infra/create-research-ontology.py first."
        )
    return url


def require_connection(credential, connection_name: str) -> None:
    """Fail early when the OAuth2 ontology connection has not been created yet."""
    token = credential.get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com{os.environ['AZURE_AI_PROJECT_ID']}"
        f"/connections/{connection_name}?api-version=2025-04-01-preview"
    )
    response = httpx.get(
        url, headers={"Authorization": "Bearer " + token}, timeout=60
    )
    if response.status_code == 404:
        raise RuntimeError(
            f"Project connection '{connection_name}' does not exist. Run "
            "infra/create-fabriciq-ontology-connection.py first: the ontology requires a "
            "delegated OAuth2 connection, not the project's managed identity."
        )
    response.raise_for_status()
    auth_type = response.json().get("properties", {}).get("authType")
    if auth_type != "OAuth2":
        raise RuntimeError(
            f"Project connection '{connection_name}' uses authType '{auth_type}'. "
            "Fabric IQ requires OAuth2; recreate it with "
            "infra/create-fabriciq-ontology-connection.py."
        )
    print(f"Using OAuth2 connection '{connection_name}'.")


def build_tools(args: argparse.Namespace) -> list[dict]:
    """Describe the two MCP tools the agent exposes.

    The ontology is a `fabric_iq_preview` server bound to the OAuth2 connection, so Fabric
    receives a delegated user token and enforces that user's Fabric permissions.
    """
    return [
        {
            "type": "mcp",
            "server_label": "research_knowledge",
            "server_url": knowledge_base_mcp_url(args.knowledge_base_name),
            "server_description": (
                "Semantic retrieval over the research papers themselves: contributions, "
                "methods, datasets, tasks, metrics and reported limitations."
            ),
            "project_connection_id": args.kb_connection_name,
            "allowed_tools": ["knowledge_base_retrieve"],
            "require_approval": "never",
        },
        {
            "type": "fabric_iq_preview",
            "server_label": "research_ontology",
            "server_url": ontology_mcp_url(),
            "project_connection_id": args.ontology_connection_name,
            "require_approval": "never",
        },
    ]


def upsert_agent(client: httpx.Client, endpoint: str, args: argparse.Namespace) -> dict:
    """Create the agent, or add a new version when it already exists."""
    definition = {
        "kind": "prompt",
        "model": args.model,
        "instructions": INSTRUCTIONS,
        "tools": build_tools(args),
    }
    existing = client.get(
        f"{endpoint}/agents/{args.agent_name}",
        params={"api-version": API_VERSION},
    )
    if existing.status_code == 200:
        response = client.post(
            f"{endpoint}/agents/{args.agent_name}/versions",
            params={"api-version": API_VERSION},
            json={"definition": definition, "description": DESCRIPTION},
        )
        response.raise_for_status()
        print(
            f"Added version {response.json()['version']} to existing agent "
            f"'{args.agent_name}'."
        )
    else:
        response = client.post(
            f"{endpoint}/agents",
            params={"api-version": API_VERSION},
            json={
                "name": args.agent_name,
                "definition": definition,
                "description": DESCRIPTION,
            },
        )
        response.raise_for_status()
        print(f"Created agent '{args.agent_name}'.")
    return response.json()


def main() -> None:
    """Create or update the ontology-backed research agent."""
    load_dotenv(dotenv_path=ENV_PATH, override=True)

    args = parse_args()
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    credential = AzureDeveloperCliCredential(tenant_id=os.environ["AZURE_TENANT_ID"])

    if args.grant_fabric_role:
        grant_fabric_workspace_role(credential)

    if not args.skip_connections:
        ensure_connection(
            credential,
            args.kb_connection_name,
            knowledge_base_mcp_url(args.knowledge_base_name),
            "https://search.azure.com/",
        )
        # The ontology connection is intentionally not written here. It carries OAuth2
        # client credentials and a registered callback, so it is owned by
        # infra/create-fabriciq-ontology-connection.py. Rewriting it as a managed-identity
        # connection would silently break the delegated flow the ontology requires.
        require_connection(credential, args.ontology_connection_name)

    token = credential.get_token("https://ai.azure.com/.default").token
    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
        follow_redirects=True,
    ) as client:
        result = upsert_agent(client, endpoint, args)

    print(f"Agent '{args.agent_name}' is ready in the Foundry agent playground.")
    print(f"  model: {args.model}")
    print("  tools: research_knowledge (Foundry IQ), research_ontology (Fabric IQ)")
    if identity := result.get("instance_identity"):
        print(f"  instance identity: {identity.get('principal_id')}")


if __name__ == "__main__":
    main()
