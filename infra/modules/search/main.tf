# RBAC-only Search: authOptions must be omitted when disableLocalAuth=true.
# https://learn.microsoft.com/azure/templates/microsoft.search/2025-05-01/searchservices
resource "azapi_resource" "service" {
  type      = "Microsoft.Search/searchServices@2025-05-01"
  name      = var.name
  parent_id = var.resource_group_id
  location  = var.location
  tags      = var.tags

  body = {
    sku = { name = var.sku }
    properties = {
      replicaCount        = 1
      partitionCount      = 1
      hostingMode         = "Default"
      publicNetworkAccess = "Enabled"
      disableLocalAuth    = true
      semanticSearch      = "disabled"
    }
  }
}

# Index definition CRUD requires Search Service Contributor. Data Contributor is
# insufficient. The execution identity must already be authorized to assign RBAC.
resource "azapi_resource" "execution_schema_access" {
  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", lower("${azapi_resource.service.id}/${var.execution_principal_id}/${local.service_manager_role}"))
  parent_id = azapi_resource.service.id

  body = {
    properties = {
      principalId      = var.execution_principal_id
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${local.service_manager_role}"
    }
  }
}

resource "azapi_resource" "operator_access" {
  for_each = local.operator_roles

  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", lower("${azapi_resource.service.id}/${local.operator_principal_id}/${each.value}"))
  parent_id = azapi_resource.service.id

  body = {
    properties = merge({
      principalId      = local.operator_principal_id
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value}"
    }, local.operator_principal_type == null ? {} : { principalType = local.operator_principal_type })
  }
}

# Supported by AzAPI 2.12.0, using Search audience tokens rather than ARM tokens.
# https://github.com/Azure/terraform-provider-azapi/blob/v2.12.0/examples/resources/azapi_data_plane_resource/Microsoft.Search_searchServices_indexes/main.tf
# https://learn.microsoft.com/rest/api/searchservice/indexes/create-or-update?view=rest-searchservice-2024-07-01
resource "azapi_data_plane_resource" "index" {
  count = var.create_index ? 1 : 0

  type      = "Microsoft.Search/searchServices/indexes@2024-07-01"
  name      = var.index_name
  parent_id = "${azapi_resource.service.name}.search.windows.net"

  body = {
    name = var.index_name
    fields = [
      { name = "id", type = "Edm.String", key = true, searchable = false, filterable = true, sortable = false, facetable = false, retrievable = true },
      { name = "title", type = "Edm.String", key = false, searchable = true, filterable = false, sortable = false, facetable = false, retrievable = true },
      { name = "content", type = "Edm.String", key = false, searchable = true, filterable = false, sortable = false, facetable = false, retrievable = true },
      { name = "url", type = "Edm.String", key = false, searchable = false, filterable = false, sortable = false, facetable = false, retrievable = true },
    ]
  }

  # Hidden data-plane permission dependency: ARM service creation alone does not
  # authorize the caller. Reverse ordering also keeps CRUD permission for deletion.
  depends_on = [azapi_resource.execution_schema_access]

  # AzAPI 2.12's initial existence GET bypasses retry. This covers subsequent
  # operations only; the deployment helper must gate authorization before plan/apply.
  # https://github.com/Azure/terraform-provider-azapi/blob/v2.12.0/internal/services/azapi_data_plane_resource.go
  retry = {
    error_message_regex  = ["(?i)\\b403\\b|forbidden|authorizationfailed"]
    interval_seconds     = 10
    max_interval_seconds = 60
  }

  timeouts {
    create = "30m"
    read   = "5m"
    update = "30m"
    delete = "30m"
  }
}
