output "account_id" {
  description = "AIServices account ARM ID."
  value       = azapi_resource.account.id
}

output "account_name" {
  description = "AIServices account name."
  value       = azapi_resource.account.name
}

output "account_principal_id" {
  description = "Account system-assigned identity object ID."
  value       = azapi_resource.account.identity[0].principal_id
}

output "model_deployment_name" {
  description = "Single account-level model deployment name."
  value       = azapi_resource.model.name
}

output "project_endpoint" {
  description = "Foundry API project endpoint with its telemetry connection configured."
  value       = tostring(azapi_resource.project.output.properties.endpoints["AI Foundry API"])

  # Hosted provisioning injects telemetry from the project's App Insights
  # connection at container startup, although registration only names the project.
  depends_on = [azapi_resource.application_insights_connection]
}

output "project_id" {
  description = "Foundry project ARM ID."
  value       = azapi_resource.project.id
}

output "project_name" {
  description = "Foundry project name."
  value       = azapi_resource.project.name
}

output "project_principal_id" {
  description = "Project system-assigned identity object ID."
  value       = azapi_resource.project.identity[0].principal_id
}

output "search_project_connection_id" {
  description = "Shared native CognitiveSearch AAD connection ARM ID."
  value       = azapi_resource.search_connection.id
}
