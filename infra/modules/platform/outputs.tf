output "application_insights_connection_string" {
  description = "Server-side telemetry connection string; retained in protected Terraform state."
  value       = tostring(azapi_resource.application_insights.output.properties.ConnectionString)
  sensitive   = true
}

output "application_insights_id" {
  description = "Workspace-based Application Insights ARM ID."
  value       = azapi_resource.application_insights.id
}

output "container_apps_environment_id" {
  description = "Consumption-capable managed environment ARM ID; no Container App yet."
  value       = azapi_resource.environment.id
}

output "log_analytics_workspace_id" {
  description = "Shared Log Analytics workspace ARM ID."
  value       = azapi_resource.logs.id
}

output "registry_id" {
  description = "Shared Basic ACR ARM ID."
  value       = azapi_resource.registry.id
}

output "registry_login_server" {
  description = "Registry hostname returned by ARM."
  value       = tostring(azapi_resource.registry.output.properties.loginServer)
}

output "registry_name" {
  description = "Shared Basic ACR name."
  value       = azapi_resource.registry.name
}

output "resource_group_id" {
  description = "Application resource group ARM ID."
  value       = azapi_resource.resource_group.id
}

output "resource_group_name" {
  description = "Application resource group name."
  value       = azapi_resource.resource_group.name
}

output "ui_identity_client_id" {
  description = "UI identity client ID for ManagedIdentityCredential selection."
  value       = tostring(azapi_resource.ui_identity.output.properties.clientId)
}

output "ui_identity_id" {
  description = "UI user-assigned managed identity ARM ID."
  value       = azapi_resource.ui_identity.id
}

output "ui_identity_principal_id" {
  description = "UI user-assigned managed identity object ID."
  value       = tostring(azapi_resource.ui_identity.output.properties.principalId)
}
