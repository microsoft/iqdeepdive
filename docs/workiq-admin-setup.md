# Admin Setup — Work IQ

**Who this is for**: a tenant admin (Cloud Application Administrator or above). You are enabling a
developer to run the Work IQ notebooks and the `workmate-agent` against your tenant's Microsoft 365
work data.

**Time**: ~5 minutes. **Result**: a Work IQ Entra app that the notebooks sign in with and that Azure AI Search
trusts through a federated credential.

Work IQ always runs as the **signed-in user** and honors Microsoft 365 permissions and sensitivity
labels. There is no application-only mode. Work IQ usage is billed through **Copilot credits**: configure a
usage-based billing plan in Copilot Studio and assign each user to it. Users on a **Microsoft 365 Copilot
license** can also call the Work IQ gateway directly (propagation takes 15–30 minutes).

## Create the app

A Global Administrator enables the Work IQ API in the tenant once. Then run, as a Cloud Application
Administrator or an identity with the Microsoft Graph application permissions `Application.ReadWrite.All`,
`DelegatedPermissionGrant.ReadWrite.All`, and `Directory.Read.All`:

```bash
az ad sp create --id fdcc1f02-fc51-4226-8753-f668596af7f7   # Work IQ service principal, if missing
uv run python infra/create-workiq-entra.py --apply
```

The script creates a single-tenant public-client app that exposes `access_as_user`, grants tenant-wide admin
consent for `WorkIQAgent.Ask`, adds the federated credential for the Azure AI Search managed identity, and
writes `WORK_IQ_SEARCH_ENTRA_APP_ID`, `WORK_IQ_SEARCH_ENTRA_TENANT_ID`, and
`WORK_IQ_SEARCH_FEDERATED_CREDENTIAL_ID` to `.env`. The `workiq-*` notebooks use the same app for sign-in
when `ENTRA_APP_ID` is not set, so no separate app registration is needed.
## For the Foundry `work_iq_preview` tool connection

The hosted `workmate-agent` connects to Work IQ through a Foundry **`RemoteA2A`** project
connection targeting `https://workiq.svc.cloud.microsoft/a2a/`, `authType=OAuth2`, **BYO Entra app
only** (scopes `WorkIQAgent.Ask` + `offline_access`). VNet-restricted Foundry projects are not
supported. `infra/create-workiq-toolbox.py` (run by `azd up`'s postprovision hook) creates the
Entra app, the `work-iq-connection` RemoteA2A connection, and the `work-iq-tools` toolbox — and
grants the admin consent above automatically when run by a Global Administrator. See the
[Work IQ tool docs](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/work-iq).

If multiple projects share one Foundry resource, set `WORK_IQ_CONNECTION_NAME` to a unique value
before provisioning because connection names are unique across the parent resource. Override
`CUSTOM_FOUNDRY_WORKIQ_TOOLBOX_NAME` when the toolbox also needs an environment-specific name.

## Which Entra app is which

This repository uses two Work IQ Entra apps:

| App | Created by | Env vars | Used by |
|---|---|---|---|
| `RemoteA2A` connection app | `infra/create-toolbox-workiq.py` | `WORK_IQ_ENTRA_APP_ID` | `agent-toolbox-workiq`, `agent-workiq-maf` |
| Work IQ app | `infra/create-workiq-entra.py` | `WORK_IQ_SEARCH_ENTRA_*` | `foundryiq-workiq.ipynb` (Search calls Work IQ through a federated credential, billed with Copilot credits) and sign-in for the direct `workiq-*` notebooks |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 Forbidden` with no scope message | User missing the Microsoft 365 Copilot license — assign and wait 15–30 min |
| `AADSTS65001: consent required` | Re-run step 6 (admin consent) |
| `401 Unauthorized` | Token `aud` must be `api://workiq.svc.cloud.microsoft` |
| `requires a signed-in user` from the hosted agent | Work IQ needs user context — invoke via the signed-in playground or the Teams digital worker, not an app identity |
| Empty / degraded responses | License just assigned; index not ready — wait 15–30 min |
