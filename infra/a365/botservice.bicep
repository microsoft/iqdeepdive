// Azure Bot Service that relays Microsoft 365 / Teams activity to the hosted agent.
// The bot's msaAppId is the agent's blueprint (managed agent identity) client id, and the
// endpoint is the agent's activityProtocol endpoint. Requires the Microsoft.BotService
// resource provider to be registered and Owner/Contributor on the resource group.
param botName string
param displayName string
param msaAppId string
param endpoint string
param botServiceSku string = 'F0'

resource botService 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  kind: 'azurebot'
  location: 'global'
  sku: {
    name: botServiceSku
  }
  properties: {
    displayName: displayName
    endpoint: endpoint
    msaAppId: msaAppId
    msaAppTenantId: tenant().tenantId
    msaAppType: 'SingleTenant'
  }
}

// Connect the bot to Microsoft Teams so the digital worker can be used there.
resource botServiceMsTeamsChannel 'Microsoft.BotService/botServices/channels@2021-03-01' = {
  parent: botService
  location: 'global'
  name: 'MsTeamsChannel'
  properties: {
    channelName: 'MsTeamsChannel'
  }
}

output botName string = botService.name
output botAppId string = msaAppId
