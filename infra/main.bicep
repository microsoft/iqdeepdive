targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment.')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to create.')
param resourceGroupName string = 'rg-${environmentName}'

@minLength(1)
@description('Primary Azure region. It must support Foundry hosted agents and the selected models.')
param location string

@description('Object ID of the user or application running azd.')
param principalId string

@description('Principal type of the identity running azd.')
param principalType string

@description('Model deployments serialized by the azure.ai.agents azd extension.')
param aiProjectDeploymentsJson string = '[]'

@description('Enable Application Insights and Log Analytics.')
param enableMonitoring bool = true

@description('Azure AI Search SKU.')
@allowed([
  'basic'
  'standard'
  'standard2'
  'standard3'
  'storage_optimized_l1'
  'storage_optimized_l2'
])
param searchServiceSku string = 'standard'

@description('Deploy an F2 Microsoft Fabric capacity for notebook parts 3 and 5.')
param deployFabricCapacity bool = true

@description('Optional user UPN to add as a Fabric capacity administrator.')
param fabricAdminUpn string = ''

@description('Optional service-principal object ID to add as a Fabric capacity administrator.')
param fabricServicePrincipalId string = ''

var configuredDeployments = json(aiProjectDeploymentsJson)
var fallbackDeployments = [
  {
    name: 'gpt-5.4'
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4'
      version: '2026-03-05'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 50
    }
  }
  {
    name: 'text-embedding-3-large'
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-large'
      version: '1'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 30
    }
  }
]
var deployments = empty(configuredDeployments) ? fallbackDeployments : configuredDeployments
var chatDeployments = filter(deployments, deployment => deployment.model.name != 'text-embedding-3-large')
var embeddingDeployments = filter(deployments, deployment => deployment.model.name == 'text-embedding-3-large')
var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module aiProject 'core/ai/ai-project.bicep' = {
  scope: rg
  name: 'ai-project'
  params: {
    tags: tags
    location: location
    aiFoundryProjectName: 'ai-project-${environmentName}'
    principalId: principalId
    principalType: principalType
    deployments: deployments
    enableMonitoring: enableMonitoring
    searchServiceSku: searchServiceSku
  }
}

module fabricCapacity 'core/fabric/fabric-capacity.bicep' = if (deployFabricCapacity) {
  scope: rg
  name: 'fabric-capacity'
  params: {
    name: 'fabric${uniqueString(subscription().id, resourceGroupName)}'
    location: location
    adminMember: empty(fabricAdminUpn) ? principalId : fabricAdminUpn
    servicePrincipalId: fabricServicePrincipalId
    tags: tags
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_AI_ACCOUNT_ID string = aiProject.outputs.accountId
output AZURE_AI_PROJECT_ID string = aiProject.outputs.projectId
output AZURE_AI_FOUNDRY_PROJECT_ID string = aiProject.outputs.projectId
output AZURE_AI_ACCOUNT_NAME string = aiProject.outputs.aiServicesAccountName
output AZURE_AI_PROJECT_NAME string = aiProject.outputs.projectName
output AZURE_AI_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output FOUNDRY_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output MICROSOFT_FOUNDRY_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output MICROSOFT_FOUNDRY_PROJECT_ID string = aiProject.outputs.projectId

output AZURE_AI_MODEL_DEPLOYMENT_NAME string = string(chatDeployments[0].name)
output AZURE_OPENAI_CHATGPT_DEPLOYMENT string = string(chatDeployments[0].name)
output AZURE_OPENAI_CHATGPT_MODEL_NAME string = string(chatDeployments[0].model.name)
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = string(embeddingDeployments[0].name)
output AZURE_OPENAI_ENDPOINT string = aiProject.outputs.AZURE_OPENAI_ENDPOINT
output AZURE_OPENAI_SERVICE_NAME string = aiProject.outputs.aiServicesAccountName

output AZURE_AI_SEARCH_SERVICE_NAME string = aiProject.outputs.search.serviceName
output AZURE_AI_SEARCH_SERVICE_ENDPOINT string = aiProject.outputs.search.serviceEndpoint
output AZURE_SEARCH_SERVICE_NAME string = aiProject.outputs.search.serviceName
output AZURE_SEARCH_SERVICE_ENDPOINT string = aiProject.outputs.search.serviceEndpoint

output AZURE_STORAGE_CONNECTION_NAME string = aiProject.outputs.storage.connectionName
output AZURE_STORAGE_ACCOUNT_NAME string = aiProject.outputs.storage.accountName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output APPLICATIONINSIGHTS_RESOURCE_ID string = aiProject.outputs.APPLICATIONINSIGHTS_RESOURCE_ID

output FABRIC_CAPACITY_NAME string = deployFabricCapacity ? fabricCapacity!.outputs.name : ''
output FABRIC_CAPACITY_ID string = deployFabricCapacity ? fabricCapacity!.outputs.id : ''
output FABRIC_TENANT_ID string = deployFabricCapacity ? tenant().tenantId : ''
output AZURE_TENANT_ID string = tenant().tenantId
