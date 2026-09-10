locals {
  project_host = trimsuffix(trimprefix(var.project_endpoint, "https://"), "/")
  # MODEL_DEPLOYMENT_NAME is intentionally NOT shared here: each hosted/hosted_none
  # resource and the web container set their own dedicated deployment name below.
  runtime_environment = {
    HOSTED_AGENT_NAME            = var.hosted_agent_name
    PROMPT_AGENT_NAME            = var.prompt_agent_name
    SEARCH_INDEX_NAME            = var.search_index_name
    SEARCH_PROJECT_CONNECTION_ID = var.search_project_connection_id
  }
  agent_response_exports = {
    agent_id      = "id"
    agent_name    = "name"
    agent_version = "versions.latest.version"
    status        = "versions.latest.status"
    principal_id  = "instance_identity.principal_id"
  }
  # Subsequent requests only: AzAPI 2.12 does not apply this retry to initial
  # existence GETs. Agent authoring must pass the external staged readiness gate.
  permission_retry = {
    error_message_regex  = ["(?i)permissiondenied|unauthorized|authorization|forbidden|\\b403\\b"]
    interval_seconds     = 10
    max_interval_seconds = 60
  }
}
