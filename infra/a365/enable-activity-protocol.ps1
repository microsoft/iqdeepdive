#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Enable the Bot Service / Teams "activity" protocol on the hosted agent's endpoint.

.DESCRIPTION
  `azd deploy` provisions the agent with only the `responses` protocol (used by the Foundry
  playground) and `Entra` authorization. For the Agent 365 / Teams autopilot path, the agent
  endpoint must ALSO expose the `activity` protocol and accept `BotServiceRbac` authorization so
  the Azure Bot Service can call it. Without this, Teams messages reach the Bot but the agent's
  activityProtocol endpoint does not exist, so the digital worker never replies.

  This patches the agent endpoint in place (no new version) to expose both `responses` (keeps the
  playground working) and `activity`, with both `Entra` and `BotServiceRbac` auth schemes.
  Mirrors the endpoint PATCH in the FoundryA365 sample's agent-creation-script.ps1.
#>
param(
    [string]$AgentName = "workmate-agent"
)
$ErrorActionPreference = "Stop"

$projectEndpoint = (azd env get-values | Where-Object { $_ -match '^AZURE_AI_PROJECT_ENDPOINT=' }) -replace '^AZURE_AI_PROJECT_ENDPOINT="?|"?$', ''
if ([string]::IsNullOrEmpty($projectEndpoint)) { throw "Could not read AZURE_AI_PROJECT_ENDPOINT from azd env." }

$token = az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv
if ([string]::IsNullOrEmpty($token)) { throw "Failed to acquire an https://ai.azure.com token." }

$headers = @{
    "Content-Type"     = "application/json"
    "Accept"           = "application/json"
    "Authorization"    = "Bearer $token"
    "Foundry-Features" = "HostedAgents=V1Preview,AgentEndpoints=V1Preview"
}

$patchUrl = "$projectEndpoint/agents/$AgentName`?api-version=2025-11-15-preview"
$body = @{
    agent_endpoint = @{
        protocols             = @("responses", "activity")
        authorization_schemes = @(@{ type = "Entra" }, @{ type = "BotServiceRbac" })
    }
} | ConvertTo-Json -Depth 6

Write-Host "PATCH $patchUrl"
Write-Host $body

$response = Invoke-RestMethod -Uri $patchUrl -Method Patch -Headers $headers -Body $body
$protocols = $response.agent_endpoint.protocols -join ", "
Write-Host ""
Write-Host "Agent endpoint protocols now: $protocols"
