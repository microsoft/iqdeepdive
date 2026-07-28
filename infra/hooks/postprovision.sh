#!/bin/sh
set -eu

echo "Writing local development settings..."
uv run --locked python infra/setup-env.py

echo "Creating the shared Search indexes and the HR agent knowledge base..."
if [ "${ENABLE_WORK_IQ_KB_TOOLBOX:-false}" = "true" ]; then
    uv run --locked python infra/create-search-indexes.py --include-workiq
else
    uv run --locked python infra/create-search-indexes.py
fi

echo "Creating the Foundry toolbox..."
uv run --locked python infra/create-toolbox-foundryiq.py

if [ "${ENABLE_WORK_IQ_KB_TOOLBOX:-false}" = "true" ]; then
    echo "Creating the Work IQ knowledge-base toolbox..."
    uv run --locked python infra/create-toolbox-foundryiq.py \
        --toolbox-name "${CUSTOM_FOUNDRY_WORKIQ_KB_TOOLBOX_NAME:-workiq-knowledge-tools}" \
        --knowledge-base-name "${AZURE_AI_SEARCH_WORKIQ_KNOWLEDGE_BASE_NAME:-multisource-workiq-knowledge-base}" \
        --connection-name "${AZURE_AI_SEARCH_WORKIQ_KB_MCP_CONNECTION_NAME:-workiq-kb-mcp-connection}" \
        --knowledge-base-description "Retrieve the signed-in user's Microsoft 365 work context through Foundry IQ." \
        --toolbox-description "Foundry IQ knowledge-base tools backed by a Work IQ knowledge source."
fi

if [ "${ENABLE_FABRIC_ITEMS:-false}" = "true" ]; then
    if [ -z "${FABRIC_CAPACITY_ID:-}" ] && [ -z "${FABRIC_WORKSPACE_ID:-}" ]; then
        echo "ENABLE_FABRIC_ITEMS is true but neither FABRIC_CAPACITY_ID nor FABRIC_WORKSPACE_ID is set." >&2
        echo "Set ENABLE_FABRIC_CAPACITY=true to create a capacity, or set FABRIC_WORKSPACE_ID to use an existing workspace." >&2
        exit 1
    fi

    echo "Creating the optional Fabric lakehouse and ontology..."
    uv run --locked python infra/create-lakehouse.py

    echo "Creating the Fabric product review graph..."
    uv run --locked python infra/create-fabric-graph.py

    echo "Creating the web analytics lakehouse..."
    uv run --locked python infra/create-web-analytics-lakehouse.py

    echo "Creating the web analytics semantic model..."
    uv run --locked python infra/create-semantic-model.py

    echo "Creating the web analytics Power BI report..."
    uv run --locked python infra/create-web-analytics-report.py

    echo "Creating the Fabric data agent..."
    uv run --locked python infra/create-fabric-data-agent.py
fi

if [ "${ENABLE_WORK_IQ:-false}" = "true" ]; then
    echo "Creating the Work IQ Entra application, connection, and toolbox..."
    uv run --locked python infra/create-toolbox-workiq.py --apply
fi

echo "Postprovision setup complete."
