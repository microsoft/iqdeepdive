targetScope = 'resourceGroup'

@description('AI Services account name')
param aiServicesAccountName string

@description('AI project name')
param aiProjectName string

// Connection configuration type definition
type ConnectionConfig = {
  @description('Name of the connection')
  name: string

  @description('Category of the Foundry project connection')
  category: string

  @description('Target endpoint or URL for the connection')
  target: string

  @description('Authentication type')
  authType: 'AAD' | 'AccessKey' | 'AccountKey' | 'ApiKey' | 'CustomKeys' | 'ManagedIdentity' | 'None' | 'OAuth2' | 'PAT' | 'ProjectManagedIdentity' | 'SAS' | 'ServicePrincipal' | 'UsernamePassword'

  @description('Token audience for identity-based authentication')
  audience: string?

  @description('Whether the connection is shared to all users (optional, defaults to true)')
  isSharedToAll: bool?

  @description('Credentials for non-ApiKey authentication types (optional)')
  credentials: object?

  @description('Additional metadata for the connection (optional)')
  metadata: object?
}

@description('Connection configuration')
param connectionConfig ConnectionConfig

@secure()
@description('API key for ApiKey based connections (optional)')
param apiKey string = ''


// Get reference to the AI Services account and project
resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesAccountName

  resource project 'projects' existing = {
    name: aiProjectName
  }
}

// Create the connection
resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: aiAccount::project
  name: connectionConfig.name
  properties: {
    category: connectionConfig.category
    target: connectionConfig.target
    #disable-next-line BCP036
    authType: connectionConfig.authType
    audience: connectionConfig.?audience
    isSharedToAll: connectionConfig.?isSharedToAll ?? true
    credentials: connectionConfig.authType == 'ApiKey' ? {
      key: apiKey
    } : connectionConfig.?credentials
    metadata: connectionConfig.?metadata
  }
}

// Outputs
output connectionName string = connection.name
output connectionId string = connection.id
