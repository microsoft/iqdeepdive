targetScope = 'resourceGroup'

@description('Tags that will be applied to all resources.')
param tags object = {}

@description('Main location for the resources.')
param location string

@description('Name of the Foundry project.')
param aiFoundryProjectName string

@description('Model deployments managed by azd.')
param deployments deploymentsType

@description('Object ID of the user or application running azd.')
param principalId string

@description('Principal type of the identity running azd.')
param principalType string

@description('Enable Application Insights and Log Analytics.')
param enableMonitoring bool = true

@description('Azure AI Search SKU.')
param searchServiceSku string = 'standard'

var resourceToken = uniqueString(subscription().id, resourceGroup().id, location)

module logAnalytics '../monitor/loganalytics.bicep' = if (enableMonitoring) {
  name: 'logAnalytics'
  params: {
    location: location
    tags: tags
    name: 'logs-${resourceToken}'
  }
}

module applicationInsights '../monitor/applicationinsights.bicep' = if (enableMonitoring) {
  name: 'applicationInsights'
  params: {
    location: location
    tags: tags
    name: 'appi-${resourceToken}'
    logAnalyticsWorkspaceId: logAnalytics!.outputs.id
  }
}

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-account-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'ai-account-${resourceToken}'
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }

  @batchSize(1)
  resource sequentialDeployments 'deployments' = [
    for deployment in (deployments ?? []): {
      name: deployment.name
      properties: {
        model: deployment.model
      }
      sku: deployment.sku
    }
  ]

  resource project 'projects' = {
    name: aiFoundryProjectName
    location: location
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      description: 'Foundry IQ notebooks and HR hosted agent'
      displayName: 'Foundry IQ deep dive'
    }
    dependsOn: [
      sequentialDeployments
    ]
  }
}

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (enableMonitoring) {
  parent: aiAccount::project
  name: 'appi-connection'
  properties: {
    category: 'AppInsights'
    target: applicationInsights!.outputs.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: applicationInsights!.outputs.connectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsights!.outputs.id
    }
  }
}

resource localUserAzureAIUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiAccount::project
  name: guid(subscription().id, resourceGroup().id, principalId, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

resource localUserProjectManagerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiAccount::project
  name: guid(subscription().id, resourceGroup().id, principalId, 'eadc314b-1a2d-4efa-be10-5d325db5065e')
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', 'eadc314b-1a2d-4efa-be10-5d325db5065e')
  }
}

module storage '../storage/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    tags: tags
    resourceName: 'st${resourceToken}'
    connectionName: 'storage-connection'
    principalId: principalId
    principalType: principalType
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
  }
}

module azureAiSearch '../search/azure_ai_search.bicep' = {
  name: 'azure-ai-search'
  params: {
    tags: tags
    resourceName: 'search-${resourceToken}'
    azureSearchSkuName: searchServiceSku
    connectionName: 'azure-ai-search-connection'
    storageAccountResourceId: storage.outputs.storageAccountId
    containerName: 'knowledge'
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    principalId: principalId
    principalType: principalType
    location: location
  }
}

module knowledgeBaseMcpConnection 'connection.bicep' = {
  name: 'knowledge-base-mcp-connection'
  params: {
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    connectionConfig: {
      name: 'kb-mcp-connection'
      category: 'RemoteTool'
      target: 'https://${azureAiSearch.outputs.searchServiceName}.search.windows.net/knowledgebases/contoso-company-kb-minimal/mcp?api-version=2026-05-01-preview'
      authType: 'ProjectManagedIdentity'
      audience: 'https://search.azure.com/'
      isSharedToAll: true
    }
  }
}

module workIqKnowledgeBaseMcpConnection 'connection.bicep' = {
  name: 'workiq-knowledge-base-mcp-connection'
  params: {
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    connectionConfig: {
      name: 'workiq-kb-mcp-connection'
      category: 'RemoteTool'
      target: 'https://${azureAiSearch.outputs.searchServiceName}.search.windows.net/knowledgebases/multisource-workiq-knowledge-base/mcp?api-version=2026-05-01-preview'
      authType: 'ProjectManagedIdentity'
      audience: 'https://search.azure.com/'
      isSharedToAll: true
      metadata: {
        ApiType: 'Azure'
      }
    }
  }
}

output AZURE_AI_PROJECT_ENDPOINT string = aiAccount::project.properties.endpoints['AI Foundry API']
output AZURE_OPENAI_ENDPOINT string = aiAccount.properties.endpoints['OpenAI Language Model Instance API']
output accountId string = aiAccount.id
output projectId string = aiAccount::project.id
output aiServicesAccountName string = aiAccount.name
output projectName string = aiAccount::project.name
output APPLICATIONINSIGHTS_CONNECTION_STRING string = enableMonitoring ? applicationInsights!.outputs.connectionString : ''
output APPLICATIONINSIGHTS_RESOURCE_ID string = enableMonitoring ? applicationInsights!.outputs.id : ''
output search object = {
  serviceName: azureAiSearch.outputs.searchServiceName
  serviceEndpoint: 'https://${azureAiSearch.outputs.searchServiceName}.search.windows.net'
  connectionName: azureAiSearch.outputs.searchConnectionName
}
output storage object = {
  accountName: storage.outputs.storageAccountName
  connectionName: storage.outputs.storageConnectionName
}

type deploymentsType = {
  name: string
  model: {
    name: string
    format: string
    version: string
  }
  sku: {
    name: string
    capacity: int
  }
}[]?
