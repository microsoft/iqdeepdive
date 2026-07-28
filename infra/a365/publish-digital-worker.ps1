#!/usr/bin/env pwsh
# Submits the Microsoft 365 "publish" (autopilot) request for the hosted agent. This creates a
# pending agent blueprint in the Microsoft 365 admin center so an admin can approve it, after
# which the agent appears in the Agent 365 registry.
#
# Requires these environment variables (set by publish-autopilot.ps1):
#   AGENT_IDENTITY_BLUEPRINT_ID, SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, LOCATION,
#   ACCOUNT_NAME, PROJECT_NAME, AGENT_NAME
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentGuid
)

$ErrorActionPreference = "Stop"

Write-Host "Publishing digital worker: blueprintId $env:AGENT_IDENTITY_BLUEPRINT_ID agent $env:AGENT_NAME guid $AgentGuid"

# Body based on Microsoft365PublishRequest. Metadata is what a hiring admin sees in the registry.
$body = @{
    agentGuid              = $AgentGuid
    botId                  = $env:AGENT_IDENTITY_BLUEPRINT_ID
    publishAsDigitalWorker = $true
    appPublishScope        = "Tenant"
    subscriptionId         = $env:SUBSCRIPTION_ID
    agentName              = $env:AGENT_NAME
    appVersion             = "1.0.0"
    shortDescription       = "Workmate - a Work IQ digital worker."
    fullDescription        = "Workmate is a Work IQ Deep Dive autopilot (digital worker). It reasons over Microsoft 365 work data (mail, meetings, files, people) via the Work IQ toolbox and can take actions such as drafting and sending mail."
    developerName          = "aycabas"
    developerWebsiteUrl    = "https://github.com/aycabas/iqdeepdive-workiq"
    privacyUrl             = "https://privacy.microsoft.com"
    termsOfUseUrl          = "https://www.microsoft.com/legal/terms-of-use"
    useAgenticUserTemplate = $true
    agenticUserTemplate    = @{
        Id                       = "digitalWorkerTemplate"
        File                     = "agenticUserTemplateManifest.json"
        SchemaVersion            = "0.1.0-preview"
        AgentIdentityBlueprintId = $env:AGENT_IDENTITY_BLUEPRINT_ID
        CommunicationProtocol    = "activityProtocol"
    }
}

$jsonBody = $body | ConvertTo-Json -Depth 10

$aiAzureToken = az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv
if ([string]::IsNullOrEmpty($aiAzureToken)) {
    throw "Failed to acquire an https://ai.azure.com access token. Try: az login --scope https://ai.azure.com/.default"
}

$workspaceName = "$($env:ACCOUNT_NAME)@$($env:PROJECT_NAME)@AML"
$uri = "https://$($env:LOCATION).api.azureml.ms/agent-asset/v2.0/subscriptions/$($env:SUBSCRIPTION_ID)/resourceGroups/$($env:AZURE_RESOURCE_GROUP)/providers/Microsoft.MachineLearningServices/workspaces/$($workspaceName)/microsoft365/publish"

Write-Host "POST $uri"
Write-Host "Body:"
Write-Host $jsonBody

try {
    $response = Invoke-RestMethod -Uri $uri `
        -Method Post `
        -Headers @{
            "Content-Type"  = "application/json"
            "Accept"        = "application/json"
            "Authorization" = "Bearer $aiAzureToken"
        } `
        -Body $jsonBody

    Write-Host ""
    Write-Host "Response:"
    $response | ConvertTo-Json -Depth 5 | Write-Host
}
catch {
    $err = $null
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        try { $err = $_.ErrorDetails.Message | ConvertFrom-Json } catch { $err = $null }
    }
    if ($err -and $err.error.code -eq "UserError" -and $err.error.message -like "*version already exists*") {
        Write-Host "A digital worker is already published with this version. Ignoring."
    }
    else {
        throw
    }
}

Write-Host "Publish digital worker request complete."
