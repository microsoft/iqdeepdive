#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Publish the already-deployed hosted `workmate-agent` as an Agent 365 autopilot (digital worker).

.DESCRIPTION
  Our agent is deployed with `azd deploy` (not the full C# sample `azd provision`), so the
  Agent 365 registration steps were never run. This script performs them against the existing
  agent, mirroring the FoundryA365 sample post-provision flow:

    1. Register the Microsoft.BotService resource provider (idempotent).
    2. Deploy an Azure Bot Service (msaAppId = blueprint id, endpoint = activityProtocol).
    3. Submit the Microsoft 365 publish (autopilot) request -> pending blueprint in M365 admin center.
    4. Grant the blueprint SP the OAuth2 permissions its inheritable MCP scopes need.
    5. Add the current user as an owner of the blueprint application.

  After this, an admin must APPROVE the request at
  https://admin.cloud.microsoft/?#/agents/all/requested. Then run
  ./configure-blueprint-backend.ps1 (or set Bot ID in the Teams Developer Portal) and create an
  instance in Teams.

.NOTES
  Prereqs: Owner/Contributor on the resource group (Bot Service), az + azd logged in, and an
  Agent 365 / Copilot license in the tenant. Reads live values from `azd env get-values` and
  `azd ai agent show`.
#>
param(
    [string]$AgentName = "workmate-agent",
    [switch]$SkipBotService,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path "$PSScriptRoot/../.."
Push-Location $repoRoot
try {
    Write-Host "=== Reading azd environment values ===" -ForegroundColor Cyan
    $envLines = azd env get-values
    $envMap = @{}
    foreach ($line in $envLines) {
        if ($line -match '^\s*([A-Z0-9_]+)="?(.*?)"?\s*$') { $envMap[$Matches[1]] = $Matches[2] }
    }

    $subscriptionId = $envMap["AZURE_SUBSCRIPTION_ID"]
    $resourceGroup  = $envMap["AZURE_RESOURCE_GROUP"]
    $location       = $envMap["AZURE_LOCATION"]
    $projectEndpoint = $envMap["AZURE_AI_PROJECT_ENDPOINT"]

    # Derive account + project names from the project endpoint:
    # https://<account>.services.ai.azure.com/api/projects/<project>
    if ($projectEndpoint -notmatch 'https://([^.]+)\.services\.ai\.azure\.com/api/projects/([^/]+)') {
        throw "Could not parse account/project from AZURE_AI_PROJECT_ENDPOINT: $projectEndpoint"
    }
    $accountName = $Matches[1]
    $projectName = $Matches[2]

    Write-Host "=== Reading agent details ($AgentName) ===" -ForegroundColor Cyan
    $agentShow = azd ai agent show $AgentName
    function Get-Field($label) {
        $row = $agentShow | Where-Object { $_ -match "^\s*$([regex]::Escape($label))\s{2,}\S" } | Select-Object -First 1
        if ($row) { return (($row -split '\s{2,}', 2)[1]).Trim() }
        return $null
    }
    $blueprintId  = Get-Field "Blueprint Client ID"
    $agentGuid    = Get-Field "Agent GUID"
    $agentVersion = Get-Field "Version"

    if (-not $blueprintId) { throw "Could not read Blueprint Client ID from 'azd ai agent show $AgentName'." }
    if (-not $agentGuid)   { throw "Could not read Agent GUID from 'azd ai agent show $AgentName'." }

    # Export the env vars the sub-scripts expect.
    $env:AGENT_IDENTITY_BLUEPRINT_ID = $blueprintId
    $env:SUBSCRIPTION_ID             = $subscriptionId
    $env:AZURE_RESOURCE_GROUP        = $resourceGroup
    $env:LOCATION                    = $location
    $env:ACCOUNT_NAME                = $accountName
    $env:PROJECT_NAME                = $projectName
    $env:AGENT_NAME                  = $AgentName
    $env:AGENT_VERSION               = $agentVersion

    Write-Host ""
    Write-Host "Subscription : $subscriptionId"
    Write-Host "ResourceGroup: $resourceGroup"
    Write-Host "Location     : $location"
    Write-Host "Account      : $accountName"
    Write-Host "Project      : $projectName"
    Write-Host "Agent        : $AgentName (v$agentVersion, guid $agentGuid)"
    Write-Host "Blueprint    : $blueprintId"
    Write-Host ""

    if ($WhatIf) { Write-Host "-WhatIf: resolved values only, no changes made." -ForegroundColor Yellow; return }

    # 0) Enable the Teams "activity" protocol + BotServiceRbac auth on the agent endpoint.
    #    azd deploy only enables the `responses` protocol; the Bot Service needs `activity`.
    Write-Host "=== [0/5] Enabling activity protocol on agent endpoint ===" -ForegroundColor Cyan
    & "$PSScriptRoot/enable-activity-protocol.ps1" -AgentName $AgentName

    # 1) Register Microsoft.BotService provider (idempotent).
    Write-Host "=== [1/5] Registering Microsoft.BotService provider ===" -ForegroundColor Cyan
    $state = az provider show --namespace Microsoft.BotService --query registrationState -o tsv 2>$null
    if ($state -ne "Registered") {
        az provider register --namespace Microsoft.BotService | Out-Null
        Write-Host "Registration requested (was: $state). This can take a few minutes."
    } else { Write-Host "Already registered." }

    # 2) Deploy the Azure Bot Service.
    if (-not $SkipBotService) {
        Write-Host "=== [2/5] Deploying Azure Bot Service ===" -ForegroundColor Cyan
        $botName = "$AgentName-bot"
        $endpoint = "https://$accountName.services.ai.azure.com/api/projects/$projectName/agents/$AgentName/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"
        az deployment group create `
            --resource-group $resourceGroup `
            --name "a365-botservice-$AgentName" `
            --template-file "$PSScriptRoot/botservice.bicep" `
            --parameters botName=$botName displayName="Workmate" msaAppId=$blueprintId endpoint=$endpoint `
            --only-show-errors | Out-Null
        Write-Host "Bot Service '$botName' deployed."
    } else { Write-Host "=== [2/5] Skipping Azure Bot Service (-SkipBotService) ===" -ForegroundColor Yellow }

    # 3) Submit the Microsoft 365 publish (autopilot) request.
    Write-Host "=== [3/5] Submitting Microsoft 365 publish (autopilot) request ===" -ForegroundColor Cyan
    & "$PSScriptRoot/publish-digital-worker.ps1" -AgentGuid $agentGuid

    # 4) OAuth2 grants for the blueprint SP.
    Write-Host "=== [4/5] Granting OAuth2 permissions to blueprint SP ===" -ForegroundColor Cyan
    & "$PSScriptRoot/create-blueprintsp-oauth2-grants.ps1"

    # 5) Add current user as blueprint owner.
    Write-Host "=== [5/5] Adding current user as blueprint owner ===" -ForegroundColor Cyan
    & "$PSScriptRoot/add-current-user-as-blueprint-owner.ps1"

    Write-Host ""
    Write-Host "Done. Next steps:" -ForegroundColor Green
    Write-Host "  1. Admin approves the pending blueprint at https://admin.cloud.microsoft/?#/agents/all/requested"
    Write-Host "  2. Configure the backend: ./infra/a365/configure-blueprint-backend.ps1 (or set Bot ID = $blueprintId in Teams Developer Portal)"
    Write-Host "  3. In Teams: Apps -> Agents for your team -> find '$AgentName' -> create an instance"
}
finally {
    Pop-Location
}
