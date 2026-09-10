output "hosted_agent_identity_principal_id" {
  description = "Platform-created hosted agent identity, NOT the project MI. May remain null until provisioning completes; no extra grant is needed for project inference."
  value       = try(tostring(azapi_data_plane_resource.hosted.output.principal_id), null)
}

output "hosted_agent_name" {
  description = "Registered hosted agent name (low reasoning)."
  value       = azapi_data_plane_resource.hosted.name
}

output "hosted_agent_version" {
  description = "Exact service-assigned hosted version for route-agents; registration alone does not prove readiness or endpoint selection."
  value       = tostring(azapi_data_plane_resource.hosted.output.agent_version)
}

output "hosted_none_agent_identity_principal_id" {
  description = "Platform-created hosted (no reasoning) agent identity, NOT the project MI."
  value       = try(tostring(azapi_data_plane_resource.hosted_none.output.principal_id), null)
}

output "hosted_none_agent_name" {
  description = "Registered hosted agent name (no reasoning)."
  value       = azapi_data_plane_resource.hosted_none.name
}

output "hosted_none_agent_version" {
  description = "Exact service-assigned hosted (no reasoning) version for route-agents."
  value       = tostring(azapi_data_plane_resource.hosted_none.output.agent_version)
}

output "prompt_agent_name" {
  description = "Registered native Search prompt agent name (low reasoning)."
  value       = azapi_data_plane_resource.prompt.name
}

output "prompt_agent_version" {
  description = "Exact service-assigned prompt version for the separately approved route-agents step."
  value       = tostring(azapi_data_plane_resource.prompt.output.agent_version)
}

output "prompt_none_agent_name" {
  description = "Registered native Search prompt agent name (no reasoning)."
  value       = azapi_data_plane_resource.prompt_none.name
}

output "prompt_none_agent_version" {
  description = "Exact service-assigned prompt (no reasoning) version for the separately approved route-agents step."
  value       = tostring(azapi_data_plane_resource.prompt_none.output.agent_version)
}

output "ui_url" {
  description = "Anonymous public demo UI HTTPS URL. Only public documents belong in this demo."
  value       = "https://${azapi_resource.web.output.properties.configuration.ingress.fqdn}"
}
