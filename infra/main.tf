# Bootstrap this same root with deploy_workloads=false, build/push real images,
# then review a separate plan with deploy_workloads=true and immutable digests.
# Before workload plan AND apply, the deployment helper must verify execution-
# identity authorization with Search index and both Foundry agent GETs. ARM RBAC
# completion and image-build time alone do not prove propagation. Direct Terraform
# commands bypass this required readiness gate and can fail the initial GET.
module "platform" {
  source = "./modules/platform"

  location        = var.location
  name_token      = local.name_token
  subscription_id = var.subscription_id
  tags            = local.tags
}

module "search" {
  source = "./modules/search"

  create_index           = var.deploy_workloads
  execution_principal_id = data.azapi_client_config.current.object_id
  index_name             = var.search_index_name
  location               = var.location
  name                   = "srch-${local.name_token}"
  operator               = var.search_operator
  resource_group_id      = module.platform.resource_group_id
  sku                    = var.search_sku
  subscription_id        = var.subscription_id
  tags                   = local.tags
}

module "foundry" {
  source = "./modules/foundry"

  application_insights_connection_string = module.platform.application_insights_connection_string
  application_insights_id                = module.platform.application_insights_id
  location                               = var.location
  model_capacity                         = var.model_capacity
  model_deployment_name                  = var.model_deployment_name
  model_name                             = var.model_name
  model_version                          = var.model_version
  name_token                             = local.name_token
  resource_group_id                      = module.platform.resource_group_id
  search_endpoint                        = module.search.endpoint
  search_id                              = module.search.id
  tags                                   = local.tags
}

module "access" {
  source = "./modules/access"

  account_id             = module.foundry.account_id
  account_principal_id   = module.foundry.account_principal_id
  execution_principal_id = data.azapi_client_config.current.object_id
  project_id             = module.foundry.project_id
  project_principal_id   = module.foundry.project_principal_id
  registry_id            = module.platform.registry_id
  search_id              = module.search.id
  subscription_id        = var.subscription_id
  ui_principal_id        = module.platform.ui_identity_principal_id
}

module "workloads" {
  count  = var.deploy_workloads ? 1 : 0
  source = "./modules/workloads"

  agent_config                           = local.agent_config
  application_insights_connection_string = module.platform.application_insights_connection_string
  container_apps_environment_id          = module.platform.container_apps_environment_id
  hosted_agent_name                      = var.hosted_agent_name
  hosted_agent_name_none                 = var.hosted_agent_name_none
  hosted_image                           = var.hosted_image
  location                               = var.location
  model_deployment_name                  = module.foundry.model_deployment_name
  name_token                             = local.name_token
  project_endpoint                       = module.foundry.project_endpoint
  prompt_agent_name                      = var.prompt_agent_name
  prompt_agent_name_none                 = var.prompt_agent_name_none
  registry_login_server                  = module.platform.registry_login_server
  resource_group_id                      = module.platform.resource_group_id
  search_index_name                      = module.search.index_name
  search_project_connection_id           = module.foundry.search_project_connection_id
  tags                                   = local.tags
  ui_identity_client_id                  = module.platform.ui_identity_client_id
  ui_identity_id                         = module.platform.ui_identity_id
  web_image                              = var.web_image

  # Hidden permission ordering only: this module contains RBAC assignments and
  # nothing else. Authoring, platform image pulls, inference and native Search
  # must be authorized before agent provisioning or the UI revision is created.
  depends_on = [module.access]
}
