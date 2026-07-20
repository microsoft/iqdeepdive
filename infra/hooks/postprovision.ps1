$ErrorActionPreference = "Stop"

Write-Host "Writing local development settings..."
uv run --locked python infra/setup-env.py

Write-Host "Creating the shared Search indexes and the HR agent knowledge base..."
uv run --locked python infra/create-search-indexes.py

Write-Host "Creating the Foundry toolbox..."
uv run --locked python infra/create-toolbox.py

if ($env:FABRIC_CAPACITY_ID -or $env:FABRIC_WORKSPACE_ID) {
    Write-Host "Creating the optional Fabric lakehouse and ontology..."
    uv run --locked python infra/create-lakehouse.py

    Write-Host "Creating the Fabric IQ toolbox..."
    uv run --locked python infra/create-fabric-toolbox.py
}

if ($env:ENABLE_WORK_IQ -eq "true") {
    Write-Host "Creating the Work IQ Entra application, connection, and toolbox..."
    uv run --locked python infra/create-workiq-toolbox.py --apply
}

Write-Host "Postprovision setup complete."
