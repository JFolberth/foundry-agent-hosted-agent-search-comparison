resource "azapi_resource" "resource_group" {
  type      = "Microsoft.Resources/resourceGroups@2022-09-01"
  name      = "rg-${var.name_token}"
  parent_id = "/subscriptions/${var.subscription_id}"
  location  = var.location
  tags      = var.tags
  body      = {}
}

resource "azapi_resource" "logs" {
  type      = "Microsoft.OperationalInsights/workspaces@2023-09-01"
  name      = "law-${var.name_token}"
  parent_id = azapi_resource.resource_group.id
  location  = var.location
  tags      = var.tags

  body = {
    properties = {
      sku             = { name = "PerGB2018" }
      retentionInDays = 30
      features        = { disableLocalAuth = true }
    }
  }
}

resource "azapi_resource" "application_insights" {
  type      = "Microsoft.Insights/components@2020-02-02"
  name      = "appi-${var.name_token}"
  parent_id = azapi_resource.resource_group.id
  location  = var.location
  tags      = var.tags

  body = {
    kind = "web"
    properties = {
      Application_Type    = "web"
      WorkspaceResourceId = azapi_resource.logs.id
      DisableIpMasking    = false
    }
  }
  response_export_values = ["properties.ConnectionString"]
}

# Explicit LegacyRegistryPermissions keeps AcrPull effective, unlike ABAC mode.
# https://learn.microsoft.com/azure/templates/microsoft.containerregistry/2025-11-01/registries
resource "azapi_resource" "registry" {
  type      = "Microsoft.ContainerRegistry/registries@2025-11-01"
  name      = "cr${replace(var.name_token, "-", "")}"
  parent_id = azapi_resource.resource_group.id
  location  = var.location
  tags      = var.tags

  body = {
    sku = { name = "Basic" }
    properties = {
      adminUserEnabled     = false
      anonymousPullEnabled = false
      publicNetworkAccess  = "Enabled"
      roleAssignmentMode   = "LegacyRegistryPermissions"
      # Hosted image pulls require ACR to accept ARM-audience Entra tokens.
      # https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions
      policies = {
        azureADAuthenticationAsArmPolicy = { status = "enabled" }
      }
    }
  }
  response_export_values = ["properties.loginServer"]
}

# Azure Monitor destination avoids retrieving or storing Log Analytics shared keys.
# https://learn.microsoft.com/azure/container-apps/log-options
resource "azapi_resource" "environment" {
  type      = "Microsoft.App/managedEnvironments@2025-01-01"
  name      = "cae-${var.name_token}"
  parent_id = azapi_resource.resource_group.id
  location  = var.location
  tags      = var.tags

  body = {
    properties = {
      appLogsConfiguration = { destination = "azure-monitor" }
      workloadProfiles = [{
        name                = "Consumption"
        workloadProfileType = "Consumption"
      }]
    }
  }
}

resource "azapi_resource" "environment_logs" {
  type      = "Microsoft.Insights/diagnosticSettings@2021-05-01-preview"
  name      = "environment-to-workspace"
  parent_id = azapi_resource.environment.id

  list_unique_id_property = {
    "properties.logs" = "category"
  }

  body = {
    properties = {
      workspaceId = azapi_resource.logs.id
      # Match Azure's persisted defaults while tracking every category's enabled
      # flag. Future categories stay visible as drift rather than being ignored.
      logs = [
        for category, enabled in {
          AppEnvSessionConsoleLogs   = false
          AppEnvSessionLifeCycleLogs = false
          AppEnvSessionPoolEventLogs = false
          AppEnvSpringAppConsoleLogs = false
          ContainerAppConsoleLogs    = true
          ContainerAppHTTPLogs       = false
          ContainerAppSystemLogs     = true
          } : {
          category      = category
          categoryGroup = null
          enabled       = enabled
          retentionPolicy = {
            days    = 0
            enabled = false
          }
        }
      ]
    }
  }
}

resource "azapi_resource" "ui_identity" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31"
  name      = "id-ui-${var.name_token}"
  parent_id = azapi_resource.resource_group.id
  location  = var.location
  tags      = var.tags
  body      = {}

  response_export_values = ["properties.clientId", "properties.principalId"]
}
