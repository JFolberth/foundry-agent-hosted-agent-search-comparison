output "account_id" {
  description = "Foundry AIServices account ARM ID."
  value       = module.foundry.account_id
}

output "account_name" {
  description = "Foundry account name."
  value       = module.foundry.account_name
}

output "account_principal_id" {
  description = "Foundry account system-assigned identity object ID."
  value       = module.foundry.account_principal_id
}

output "application_insights_connection_string" {
  description = "Telemetry connection string for server-side runtime only; do not expose in browser configuration."
  value       = module.platform.application_insights_connection_string
  sensitive   = true
}

output "application_insights_id" {
  description = "Workspace-based App Insights ARM ID."
  value       = module.platform.application_insights_id
}

output "container_apps_environment_id" {
  description = "Managed environment ARM ID for the later UI container deployment."
  value       = module.platform.container_apps_environment_id
}

output "deploy_workloads" {
  description = "Whether this state includes workloads; switching to false would remove them."
  value       = var.deploy_workloads
}

output "hosted_agent_identity_principal_id" {
  description = "Platform-created hosted identity object ID, possibly null until provisioning; never substitute the project managed identity."
  value       = var.deploy_workloads ? module.workloads[0].hosted_agent_identity_principal_id : null
}

output "hosted_agent_name" {
  description = "Registered hosted agent name, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].hosted_agent_name : null
}

output "hosted_agent_version" {
  description = "Exact hosted version for routing, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].hosted_agent_version : null
}

output "hosted_none_agent_identity_principal_id" {
  description = "Platform-created hosted (no reasoning) identity object ID, possibly null until provisioning; never substitute the project managed identity."
  value       = var.deploy_workloads ? module.workloads[0].hosted_none_agent_identity_principal_id : null
}

output "hosted_none_agent_name" {
  description = "Registered hosted (no reasoning) agent name, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].hosted_none_agent_name : null
}

output "hosted_none_agent_version" {
  description = "Exact hosted (no reasoning) version for routing, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].hosted_none_agent_version : null
}

output "location" {
  description = "Azure region shared by foundation resources."
  value       = var.location
}

output "log_analytics_workspace_id" {
  description = "Single shared Log Analytics workspace ARM ID."
  value       = module.platform.log_analytics_workspace_id
}

output "model_deployment_name" {
  description = "Legacy single account-level model deployment name; orphaned/unused now that each agent has its own dedicated deployment (see model_deployment_names)."
  value       = module.foundry.model_deployment_name
}

output "model_deployment_names" {
  description = "Dedicated per-agent-side model deployment names (prompt, prompt_none, hosted, hosted_none), null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].model_deployment_names : null
}

output "project_endpoint" {
  description = "Service-returned Foundry project API endpoint; use only in the server-side runtime."
  value       = module.foundry.project_endpoint
}

output "project_id" {
  description = "Foundry project ARM ID."
  value       = module.foundry.project_id
}

output "project_name" {
  description = "Foundry project name."
  value       = module.foundry.project_name
}

output "project_principal_id" {
  description = "Project system-assigned identity object ID for hosted runtime permissions."
  value       = module.foundry.project_principal_id
}

output "prompt_agent_name" {
  description = "Registered prompt agent name, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].prompt_agent_name : null
}

output "prompt_agent_version" {
  description = "Exact prompt version for routing, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].prompt_agent_version : null
}

output "prompt_none_agent_name" {
  description = "Registered prompt (no reasoning) agent name, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].prompt_none_agent_name : null
}

output "prompt_none_agent_version" {
  description = "Exact prompt (no reasoning) version for routing, null during foundation bootstrap."
  value       = var.deploy_workloads ? module.workloads[0].prompt_none_agent_version : null
}

output "registry_id" {
  description = "Shared Basic ACR ARM ID, configured for legacy registry RBAC."
  value       = module.platform.registry_id
}

output "registry_login_server" {
  description = "Service-returned ACR hostname for building and pushing real workload images."
  value       = module.platform.registry_login_server
}

output "registry_name" {
  description = "Shared ACR resource name."
  value       = module.platform.registry_name
}

output "resource_group_id" {
  description = "Application resource group ARM ID."
  value       = module.platform.resource_group_id
}

output "resource_group_name" {
  description = "Predictable dev resource group name including the stable uniqueness suffix."
  value       = module.platform.resource_group_name
}

output "search_endpoint" {
  description = "Public-cloud Search endpoint; requires Entra authorization despite public network access."
  value       = module.search.endpoint
}

output "search_index_name" {
  description = "Configured name for authorization GETs during foundation; managed lexical index name when workloads are enabled. Document ingestion remains manual."
  value       = module.search.index_name
}

output "search_project_connection_id" {
  description = "AAD CognitiveSearch project connection ARM ID, shared by both future native agent tools."
  value       = module.foundry.search_project_connection_id
}

output "search_query_type" {
  description = "Required native-tool query type for this lexical index; do not use the tool's vector-semantic default."
  value       = "simple"
}

output "search_service_id" {
  description = "Azure AI Search service ARM ID."
  value       = module.search.id
}

output "ui_identity_client_id" {
  description = "Client ID for selecting the UI backend's user-assigned managed identity."
  value       = module.platform.ui_identity_client_id
}

output "ui_identity_id" {
  description = "User-assigned identity ARM ID to attach to the future UI Container App."
  value       = module.platform.ui_identity_id
}

output "ui_identity_principal_id" {
  description = "UI managed identity object ID; agent invocation and ACR pull only, not browser credentials."
  value       = module.platform.ui_identity_principal_id
}

output "ui_url" {
  description = "Anonymous public demo UI URL, null until workload deployment."
  value       = var.deploy_workloads ? module.workloads[0].ui_url : null
}

output "workload_agent_names" {
  description = "Always-known configured names for authorization preflight GETs; does not imply agents exist."
  value = {
    hosted      = var.hosted_agent_name
    prompt      = var.prompt_agent_name
    hosted_none = var.hosted_agent_name_none
    prompt_none = var.prompt_agent_name_none
  }
}
