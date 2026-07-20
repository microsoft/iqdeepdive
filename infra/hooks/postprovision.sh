#!/bin/sh
set -eu

echo "Writing local development settings..."
uv run --locked python infra/setup-env.py

echo "Creating the shared Search indexes and the HR agent knowledge base..."
uv run --locked python infra/create-search-indexes.py

echo "Creating the Foundry toolbox..."
uv run --locked python infra/create-toolbox.py

if [ -n "${FABRIC_CAPACITY_ID:-}" ] || [ -n "${FABRIC_WORKSPACE_ID:-}" ]; then
    echo "Creating the optional Fabric lakehouse and ontology..."
    uv run --locked python infra/create-lakehouse.py
fi

echo "Postprovision setup complete."
