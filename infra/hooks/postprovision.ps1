$ErrorActionPreference = "Stop"

Write-Host "Writing local development settings..."
uv run --locked python infra/setup-env.py

Write-Host "Creating the shared Search indexes and the HR agent knowledge base..."
if ($env:ENABLE_WORK_IQ_KB_TOOLBOX -eq "true") {
    Write-Host "Creating the Work IQ Entra application and federated credential for Azure AI Search..."
    uv run --locked python infra/create-workiq-entra.py --apply
    uv run --locked python infra/create-search-indexes.py --include-workiq
} else {
    uv run --locked python infra/create-search-indexes.py
}

Write-Host "Creating the Foundry toolbox..."
uv run --locked python infra/create-toolbox-foundryiq.py

if ($env:ENABLE_WORK_IQ_KB_TOOLBOX -eq "true") {
    Write-Host "Creating the Work IQ knowledge-base toolbox..."
    $toolboxName = if ($env:CUSTOM_FOUNDRY_WORKIQ_KB_TOOLBOX_NAME) { $env:CUSTOM_FOUNDRY_WORKIQ_KB_TOOLBOX_NAME } else { "workiq-knowledge-tools" }
    $knowledgeBaseName = if ($env:AZURE_AI_SEARCH_WORKIQ_KNOWLEDGE_BASE_NAME) { $env:AZURE_AI_SEARCH_WORKIQ_KNOWLEDGE_BASE_NAME } else { "multisource-workiq-knowledge-base" }
    $connectionName = if ($env:AZURE_AI_SEARCH_WORKIQ_KB_MCP_CONNECTION_NAME) { $env:AZURE_AI_SEARCH_WORKIQ_KB_MCP_CONNECTION_NAME } else { "workiq-kb-mcp-connection" }
    uv run --locked python infra/create-toolbox-foundryiq.py `
        --toolbox-name $toolboxName `
        --knowledge-base-name $knowledgeBaseName `
        --connection-name $connectionName `
        --knowledge-base-description "Retrieve the signed-in user's Microsoft 365 work context through Foundry IQ." `
        --toolbox-description "Foundry IQ knowledge-base tools backed by a Work IQ knowledge source."
}

if ($env:ENABLE_FABRIC_ITEMS -eq "true") {
    if (-not $env:FABRIC_CAPACITY_ID -and -not $env:FABRIC_WORKSPACE_ID) {
        Write-Error "ENABLE_FABRIC_ITEMS is true but neither FABRIC_CAPACITY_ID nor FABRIC_WORKSPACE_ID is set. Set ENABLE_FABRIC_CAPACITY=true to create a capacity, or set FABRIC_WORKSPACE_ID to use an existing workspace."
    }

    Write-Host "Creating the optional Fabric lakehouse and ontology..."
    uv run --locked python infra/create-lakehouse.py

    Write-Host "Creating the Fabric product review graph..."
    uv run --locked python infra/create-fabric-graph.py

    Write-Host "Creating the web analytics lakehouse..."
    uv run --locked python infra/create-web-analytics-lakehouse.py

    Write-Host "Creating the web analytics semantic model..."
    uv run --locked python infra/create-semantic-model.py

    Write-Host "Creating the web analytics Power BI report..."
    uv run --locked python infra/create-web-analytics-report.py

    Write-Host "Creating the Fabric data agent..."
    uv run --locked python infra/create-fabric-data-agent.py
}

if ($env:ENABLE_WORK_IQ -eq "true") {
    if ($env:ENABLE_WORK_IQ_KB_TOOLBOX -ne "true") {
        Write-Host "Creating the Work IQ Entra application..."
        uv run --locked python infra/create-workiq-entra.py --apply
    }
    Write-Host "Creating the Work IQ connection and toolbox..."
    uv run --locked python infra/create-toolbox-workiq.py --apply
}

Write-Host "Postprovision setup complete."
