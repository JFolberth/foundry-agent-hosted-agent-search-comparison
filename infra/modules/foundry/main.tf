# Uses the Cognitive Services-based Foundry project model, not legacy AML hubs.
# https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2026-03-01/accounts
resource "azapi_resource" "account" {
  type      = "Microsoft.CognitiveServices/accounts@2026-03-01"
  name      = "aif-${var.name_token}"
  parent_id = var.resource_group_id
  location  = var.location
  tags      = var.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    kind = "AIServices"
    sku  = { name = "S0" }
    properties = {
      allowProjectManagement = true
      customSubDomainName    = "aif-${var.name_token}"
      disableLocalAuth       = true
      publicNetworkAccess    = "Enabled"
    }
  }
}

resource "azapi_resource" "project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2026-03-01"
  name      = "proj-${var.name_token}"
  parent_id = azapi_resource.account.id
  location  = var.location
  tags      = var.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = "Native Search agent comparison"
      description = "Shared project, model and native Search connection for a public-data demo."
    }
  }
  response_export_values = ["properties.endpoints"]
}

resource "azapi_resource" "model" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2026-03-01"
  name      = var.model_deployment_name
  parent_id = azapi_resource.account.id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.model_capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.model_name
        version = var.model_version
      }
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }
}

resource "azapi_resource" "search_connection" {
  type      = "Microsoft.CognitiveServices/accounts/projects/connections@2026-03-01"
  name      = "native-search"
  parent_id = azapi_resource.project.id

  body = {
    properties = {
      category = "CognitiveSearch"
      target   = var.search_endpoint
      authType = "AAD"
      metadata = {
        ApiType    = "Azure"
        ResourceId = var.search_id
      }
    }
  }
}

# Foundry links App Insights via a connection, NOT a project property.
# 2026-03-01 explicitly supports the AppInsights category.
# https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2026-03-01/accounts/projects/connections
# https://github.com/Azure-Samples/AI-Gateway/blob/main/labs/foundry-e2e-private/modules/application-insights.bicep
resource "azapi_resource" "application_insights_connection" {
  type      = "Microsoft.CognitiveServices/accounts/projects/connections@2026-03-01"
  name      = "app-insights"
  parent_id = azapi_resource.project.id

  body = {
    properties = {
      category      = "AppInsights"
      target        = var.application_insights_id
      authType      = "ApiKey"
      isSharedToAll = false
      metadata = {
        ApiType    = "Azure"
        ResourceId = var.application_insights_id
      }
      credentials = {
        key = var.application_insights_connection_string
      }
    }
  }
}
