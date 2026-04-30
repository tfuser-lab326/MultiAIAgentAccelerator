// Azure Container Registry
param name string
param location string
param tags object = {}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    // Admin user disabled — Container Apps and the Foundry project pull images
    // via system-assigned managed identity + AcrPull role (see role-assignments.bicep).
    // `az acr build` (used by the postprovision hook) authenticates with the
    // deployer's Azure CLI credentials, not admin password.
    adminUserEnabled: false
  }
}

output name string = containerRegistry.name
output loginServer string = containerRegistry.properties.loginServer
output id string = containerRegistry.id
