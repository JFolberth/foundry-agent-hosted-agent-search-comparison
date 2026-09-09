locals {
  # Official GUIDs, not display-name lookup:
  # https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry
  # https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions
  # https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/containers#acrpull
  # https://learn.microsoft.com/azure/search/search-security-rbac
  role_ids = {
    acr_pull                   = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    acr_push                   = "8311e382-0749-4cb8-b61a-304f252e45ec"
    agent_consumer             = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
    foundry_user               = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
    search_data_contributor    = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
    search_service_contributor = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
  }

  # Retain the account identity grants from the native-tool setup instructions.
  # The same guide's 401/403 troubleshooting explicitly requires both roles on
  # the project identity; add that pair without granting UI/hosted identities.
  # https://learn.microsoft.com/azure/foundry/agents/how-to/tools/ai-search#troubleshooting
  # Hosted code authenticates with its separate platform-created instance identity.
  # Project-endpoint inference is implicit for that identity; the project MI below
  # authorizes the proxy to the account deployment and pulls the hosted image.
  # https://learn.microsoft.com/azure/foundry/agents/how-to/tools/ai-search
  assignments = {
    account_search_data = {
      principal_id = var.account_principal_id
      role_id      = local.role_ids.search_data_contributor
      scope        = var.search_id
    }
    account_search_service = {
      principal_id = var.account_principal_id
      role_id      = local.role_ids.search_service_contributor
      scope        = var.search_id
    }
    execution_agent_authoring = {
      principal_id = var.execution_principal_id
      role_id      = local.role_ids.foundry_user
      scope        = var.project_id
    }
    execution_image_push = {
      principal_id = var.execution_principal_id
      role_id      = local.role_ids.acr_push
      scope        = var.registry_id
    }
    project_acr_pull = {
      principal_id = var.project_principal_id
      role_id      = local.role_ids.acr_pull
      scope        = var.registry_id
    }
    project_model_inference = {
      principal_id = var.project_principal_id
      role_id      = local.role_ids.foundry_user
      scope        = var.account_id
    }
    project_search_data = {
      principal_id = var.project_principal_id
      role_id      = local.role_ids.search_data_contributor
      scope        = var.search_id
    }
    project_search_service = {
      principal_id = var.project_principal_id
      role_id      = local.role_ids.search_service_contributor
      scope        = var.search_id
    }
    ui_acr_pull = {
      principal_id = var.ui_principal_id
      role_id      = local.role_ids.acr_pull
      scope        = var.registry_id
    }
    ui_agent_invocation = {
      principal_id = var.ui_principal_id
      role_id      = local.role_ids.agent_consumer
      scope        = var.project_id
    }
  }
}
