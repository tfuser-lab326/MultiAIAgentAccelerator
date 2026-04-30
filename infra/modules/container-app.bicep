// Container App (used for both backend and frontend)
param name string
param location string
param tags object = {}
param containerAppsEnvironmentId string
param containerRegistryLoginServer string
param imageName string
param targetPort int
param env array = []
param secrets array = []
param healthCheckPath string = ''

@description('Whether ACR images have been built (if false, uses placeholder image)')
param useAcrImage bool = false

@description('CPU cores allocated to each container instance')
param cpu string = '0.5'

@description('Memory allocated to each container instance')
param memory string = '1Gi'

@description('Minimum number of replicas')
param minReplicas int = 0

@description('Maximum number of replicas')
param maxReplicas int = 3

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
      // Pull images from ACR using the Container App's system-assigned
      // managed identity. Requires AcrPull role on the registry (granted in
      // infra/modules/role-assignments.bicep). No admin password needed.
      // Only declared when actually pulling from ACR — on a clean first
      // deploy the image is the public MCR placeholder and the system MI
      // hasn't been granted AcrPull yet (the role assignment depends on
      // this Container App's principalId), so unconditionally registering
      // ACR here would cause the revision to fail authentication and the
      // provisioning operation to time out after 20 min.
      registries: useAcrImage ? [
        {
          server: containerRegistryLoginServer
          identity: 'system'
        }
      ] : []
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: imageName
          image: useAcrImage ? '${containerRegistryLoginServer}/${imageName}:latest' : 'mcr.microsoft.com/k8se/quickstart:latest'
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: env
          probes: useAcrImage && healthCheckPath != '' ? [
            {
              type: 'Liveness'
              httpGet: {
                path: healthCheckPath
                port: targetPort
              }
              periodSeconds: 30
              failureThreshold: 3
              initialDelaySeconds: 15
            }
          ] : []
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output name string = containerApp.name
output id string = containerApp.id
output principalId string = containerApp.identity.principalId
