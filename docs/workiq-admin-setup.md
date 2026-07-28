# Admin Setup — Work IQ

**Who this is for**: a tenant admin (Cloud Application Administrator or above). You are enabling a
developer to run the Work IQ notebooks and the `workmate-agent` against your tenant's Microsoft 365
work data.

**Time**: ~5 minutes. **Result**: an **App ID** and **Tenant ID** for the developer's `.env`.

Work IQ always runs as the **signed-in user** and honors Microsoft 365 permissions and sensitivity
labels. There is no application-only mode. Each user also needs a **Microsoft 365 Copilot license**
(propagation takes 15–30 minutes).

## Azure CLI (step by step)

```bash
# 1. Ensure the Work IQ service principal exists in your tenant (JIT provisioning).
az ad sp create --id fdcc1f02-fc51-4226-8753-f668596af7f7

# 2. Create the app registration as a single-tenant public client.
APP_ID=$(az ad app create \
  --display-name "Work IQ Deep Dive Client" \
  --sign-in-audience AzureADMyOrg \
  --is-fallback-public-client true \
  --query appId -o tsv)
echo "App ID: $APP_ID"

# 3. Create the service principal for the app itself.
az ad sp create --id $APP_ID

# 4. Public-client redirect URIs (localhost browser + WAM broker on Windows).
az ad app update --id $APP_ID \
  --public-client-redirect-uris \
    "http://localhost" \
    "https://login.microsoftonline.com/common/oauth2/nativeclient" \
    "ms-appx-web://microsoft.aad.brokerplugin/$APP_ID"

# 5. Add the delegated Work IQ Gateway permission (WorkIQAgent.Ask).
az ad app permission add --id $APP_ID \
  --api fdcc1f02-fc51-4226-8753-f668596af7f7 \
  --api-permissions "0b1715fd-f4bf-4c63-b16d-5be31f9847c2=Scope"

# 6. Grant tenant-wide admin consent.
az ad app permission admin-consent --id $APP_ID

# Tenant ID for the developer.
az account show --query tenantId -o tsv
```

Give the developer the **App ID** (step 2) and **Tenant ID** (step 6). They go into `.env` as
`ENTRA_APP_ID` and `ENTRA_TENANT_ID`.

## For the Foundry `work_iq_preview` tool connection

The hosted `workmate-agent` connects to Work IQ through a Foundry **`RemoteA2A`** project
connection targeting `https://workiq.svc.cloud.microsoft/a2a/`, `authType=OAuth2`, **BYO Entra app
only** (scopes `WorkIQAgent.Ask` + `offline_access`). VNet-restricted Foundry projects are not
supported. `infra/create-workiq-toolbox.py` (run by `azd up`'s postprovision hook) creates the
Entra app, the `work-iq-connection` RemoteA2A connection, and the `work-iq-tools` toolbox — and
grants the admin consent above automatically when run by a Global Administrator. See the
[Work IQ tool docs](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/work-iq).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 Forbidden` with no scope message | User missing the Microsoft 365 Copilot license — assign and wait 15–30 min |
| `AADSTS65001: consent required` | Re-run step 6 (admin consent) |
| `401 Unauthorized` | Token `aud` must be `api://workiq.svc.cloud.microsoft` |
| `requires a signed-in user` from the hosted agent | Work IQ needs user context — invoke via the signed-in playground or the Teams digital worker, not an app identity |
| Empty / degraded responses | License just assigned; index not ready — wait 15–30 min |
