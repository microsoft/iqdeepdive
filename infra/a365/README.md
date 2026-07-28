# Publish `workmate-agent` as an Agent 365 autopilot (digital worker)

Our hosted agent is deployed with `azd deploy` (not the full C# FoundryA365 sample's
`azd provision`), so the **Agent 365 registration** steps were never run. These scripts perform
them against the already-deployed agent, mirroring the sample's post-provision flow, so the agent
shows up in the **Agent 365 registry** and can be used in Microsoft Teams as a digital worker.

Reference: [Publish an autopilot in Microsoft Agent 365](https://learn.microsoft.com/azure/foundry/agents/how-to/agent-365)
and the [FoundryA365 sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/csharp/foundry-autopilot-agent).

## What it does

| Step | Script | Effect |
| ---- | ------ | ------ |
| 0 | `enable-activity-protocol.ps1` | Patch the agent endpoint to expose the **`activity`** protocol + **`BotServiceRbac`** auth (azd deploy only enables `responses`). Without this the Bot has no endpoint to call and Teams never gets a reply. |
| 1 | (inline) | Register `Microsoft.BotService` provider (idempotent). |
| 2 | `botservice.bicep` | Deploy an **Azure Bot Service** (`msaAppId` = blueprint id, endpoint = the agent's `activityProtocol` endpoint) + Teams channel. |
| 3 | `publish-digital-worker.ps1` | POST the **Microsoft 365 publish** request → creates a pending blueprint in the M365 admin center. |
| 4 | `create-blueprintsp-oauth2-grants.ps1` | Grant the blueprint SP the delegated MCP/APX scopes its inheritable tools need. |
| 5 | `add-current-user-as-blueprint-owner.ps1` | Add you as an owner of the blueprint app. |

## Run it

```powershell
# Prereqs: Owner/Contributor on the resource group, az + azd logged in,
#          an Agent 365 / Copilot license in the tenant.
./infra/a365/publish-autopilot.ps1
```

The orchestrator reads all values live from `azd env get-values` and `azd ai agent show`.
Use `-WhatIf` to print resolved values without making changes, `-SkipBotService` if the bot
already exists.

## After publishing

1. **Admin approval** — an AI Administrator / Global Administrator approves the pending blueprint
   at [admin.cloud.microsoft/#/agents/all/requested](https://admin.cloud.microsoft/?#/agents/all/requested).
   After approval the agent appears in the **Agent 365 registry**.
2. **Wire the backend (Bot ID)** — **do this in the portal UI**: open
   `https://dev.teams.microsoft.com/tools/agent-blueprint/<blueprintId>` → **Configuration** →
   set **Bot ID** = the blueprint/agent-identity client id → **Save**. The equivalent API
   (`configure-blueprint-backend.ps1`) usually returns **403** even with a valid token, so the
   portal UI is the reliable path (the upstream sample leaves this step commented out too).
3. **Use it in Teams** — Apps → **Agents for your team** → find `workmate-agent` → create an
   instance. In Teams it runs Work IQ **as its own M365 identity** (its own mailbox), not on
   behalf of the person chatting with it.
