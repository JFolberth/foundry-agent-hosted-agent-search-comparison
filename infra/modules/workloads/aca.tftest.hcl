mock_provider "azapi" {
  mock_resource "azapi_resource" {
    defaults = {
      output = { properties = { configuration = { ingress = { fqdn = "agent.internal.test.azurecontainerapps.io" } } } }
    }
  }
  mock_resource "azapi_data_plane_resource" {
    defaults = {
      output = { agent_version = "1", principal_id = "00000000-0000-0000-0000-000000000001" }
    }
  }
}

variables {
  subscription_id                        = "00000000-0000-0000-0000-000000000000"
  resource_group_id                      = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test"
  account_id                             = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/test"
  project_id                             = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/test/projects/test"
  project_endpoint                       = "https://test.services.ai.azure.com/api/projects/test"
  registry_id                            = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.ContainerRegistry/registries/test"
  registry_login_server                  = "test.azurecr.io"
  container_apps_environment_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.App/managedEnvironments/test"
  ui_identity_id                         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/ui"
  ui_identity_client_id                  = "00000000-0000-0000-0000-000000000002"
  hosted_image                           = "test.azurecr.io/search-hosted@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  web_image                              = "test.azurecr.io/search-web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  application_insights_connection_string = "test-only"
  hosted_agent_name                      = "hosted"
  hosted_agent_name_none                 = "hosted-none"
  prompt_agent_name                      = "prompt"
  prompt_agent_name_none                 = "prompt-none"
  search_index_name                      = "books"
  search_project_connection_id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/test/projects/test/connections/native-search"
  location                               = "swedencentral"
  name_token                             = "fsearch-dev-4620ae2292"
  tags                                   = {}
  model_name                             = "gpt-5.6-terra"
  model_version                          = "2026-07-09"
  model_deployments = {
    prompt      = { name = "model-prompt", capacity = 80 }
    prompt_none = { name = "model-prompt-none", capacity = 80 }
    hosted      = { name = "model-hosted", capacity = 80 }
    hosted_none = { name = "model-hosted-none", capacity = 80 }
    aca         = { name = "model-aca", capacity = 80 }
    aca_none    = { name = "model-aca-none", capacity = 80 }
  }
  agent_config = {
    instructions      = "Use the book catalog."
    reasoning_effort  = "low"
    search_query_type = "simple"
    search_top_k      = 5
    max_output_tokens = 4096
  }
}

run "bootstrap" {
  command = apply
  variables {
    aca_bootstrap = true
  }
  assert {
    condition = (
      azapi_resource.aca["aca"].name == "ca-aca-fsearch-dev-4620ae2292" &&
      azapi_resource.aca["aca_none"].name == "ca-acan-fsearch-dev-4620ae2292" &&
      alltrue([for app in azapi_resource.aca : length(app.name) <= 32])
    )
    error_message = "ACA names must fit Azure limits without renaming the existing low app."
  }
  assert {
    condition = alltrue([for app in azapi_resource.aca :
      app.identity[0].type == "SystemAssigned" &&
      app.body.properties.configuration.identitySettings[0].lifecycle == "None" &&
      length(app.body.properties.configuration.secrets) == 0 &&
      length(app.body.properties.template.containers[0].env) == 0 &&
      app.body.properties.template.containers[0].image == local.aca_bootstrap_image
    ])
    error_message = "Bootstrap must isolate system credentials and runtime settings from the pinned public image."
  }
  assert {
    condition     = azapi_resource.web.identity[0].type == "UserAssigned" && toset(azapi_resource.web.identity[0].identity_ids) == toset([var.ui_identity_id])
    error_message = "The UI must keep its existing user-assigned identity."
  }
}

run "private_runtime" {
  command = apply
  variables {
    aca_bootstrap = false
  }
  assert {
    condition = alltrue([for side, app in azapi_resource.aca :
      app.identity[0].type == "SystemAssigned" &&
      app.body.properties.configuration.registries[0].identity == "system" &&
      app.body.properties.configuration.identitySettings[0].lifecycle == "Main" &&
      app.body.properties.configuration.ingress.external == false &&
      app.body.properties.configuration.ingress.targetPort == 8088 &&
      app.body.properties.template.containers[0].image == var.hosted_image &&
      { for entry in app.body.properties.template.containers[0].env : entry.name => try(entry.value, null) }["MODEL_DEPLOYMENT_NAME"] == var.model_deployments[side].name &&
      { for entry in app.body.properties.template.containers[0].env : entry.name => try(entry.value, null) }["REASONING_EFFORT_OVERRIDE"] == local.aca_sides[side] &&
      !contains([for entry in app.body.properties.template.containers[0].env : entry.name], "AZURE_CLIENT_ID")
    ])
    error_message = "ACA runtimes must use internal ingress, their system identities, dedicated models and low/none reasoning."
  }
  assert {
    condition = alltrue([for side in keys(local.aca_sides) :
      azapi_resource.aca_assignment["${side}_foundry"].parent_id == var.project_id &&
      azapi_resource.aca_assignment["${side}_acr_pull"].parent_id == var.registry_id &&
      azapi_resource.aca_assignment["${side}_foundry"].body.properties.principalId == azapi_resource.aca[side].identity[0].principal_id
    ])
    error_message = "Each system identity must receive only its project and registry grants."
  }
}