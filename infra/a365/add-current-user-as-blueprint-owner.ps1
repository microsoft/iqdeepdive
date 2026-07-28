#!/usr/bin/env pwsh
# Adds the current az login user as an owner of the blueprint application so you can manage it in
# the Teams Developer Portal and Microsoft 365 admin center.
$ErrorActionPreference = "Stop"

$blueprintAppId = $env:AGENT_IDENTITY_BLUEPRINT_ID
if ([string]::IsNullOrEmpty($blueprintAppId)) { throw "AGENT_IDENTITY_BLUEPRINT_ID environment variable is not set." }

$currentUserId = az ad signed-in-user show --query id -o tsv
if ([string]::IsNullOrEmpty($currentUserId)) { throw "Failed to get the current signed-in user's object ID. Run 'az login' first." }
Write-Host "Current user object ID: $currentUserId"

$blueprintAppObjectId = az ad app show --id $blueprintAppId --query id -o tsv
if ([string]::IsNullOrEmpty($blueprintAppObjectId)) { throw "Failed to get application object ID for blueprint app ID $blueprintAppId" }
Write-Host "Blueprint application object ID: $blueprintAppObjectId"

$graphToken = az account get-access-token --resource https://graph.microsoft.com/ --query accessToken -o tsv
if ([string]::IsNullOrEmpty($graphToken)) { throw "Failed to acquire a Microsoft Graph access token." }

$ownerBody = @{ "@odata.id" = "https://graph.microsoft.com/v1.0/directoryObjects/$currentUserId" } | ConvertTo-Json

try {
    Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/applications/$blueprintAppObjectId/owners/`$ref" `
        -Method Post `
        -Headers @{ "Content-Type" = "application/json"; "Accept" = "application/json"; "Authorization" = "Bearer $graphToken" } `
        -Body $ownerBody | Out-Null
    Write-Host "Current user added as owner of blueprint application $blueprintAppId."
}
catch {
    $err = $null
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { try { $err = $_.ErrorDetails.Message | ConvertFrom-Json } catch { $err = $null } }
    if ($err -and $err.error.message -like "*One or more added object references already exist*") {
        Write-Host "Current user is already an owner of the blueprint application; ignoring."
    } else { throw }
}
