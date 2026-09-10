# Four dedicated per-agent model deployments (workload-scoped, not the single
# account-level foundation deployment): isolates each agent's TPM/RPM budget so
# concurrent calls from the other three agents cannot skew one agent's latency.
# deploy.py's plan validator enforces that all four share model/version/capacity
# and only the deployment name differs, keeping the comparison fair.
resource "azapi_resource" "model" {
  for_each  = var.model_deployments
  type      = "Microsoft.CognitiveServices/accounts/deployments@2026-03-01"
  name      = each.value.name
  parent_id = var.account_id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = each.value.capacity
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

  timeouts {
    create = "30m"
    update = "30m"
  }
}

# AzAPI 2.12 customizes this type to POST agents / POST agents/{name}; GET exports
# come from the agent object, whose current version is versions.latest.version.
# https://github.com/Azure/terraform-provider-azapi/blob/v2.12.0/internal/services/customization/foundry_agent_customization.go
# https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry#azure-ai-search
resource "azapi_data_plane_resource" "prompt" {
  type      = "Microsoft.Foundry/agents@v1"
  name      = var.prompt_agent_name
  parent_id = local.project_host

  body = {
    name = var.prompt_agent_name
    definition = {
      kind         = "prompt"
      model        = azapi_resource.model["prompt"].name
      instructions = var.agent_config.instructions
      reasoning    = { effort = var.agent_config.reasoning_effort }
      tools = [{
        type = "azure_ai_search"
        azure_ai_search = {
          indexes = [{
            project_connection_id = var.search_project_connection_id
            index_name            = var.search_index_name
            query_type            = var.agent_config.search_query_type
            top_k                 = var.agent_config.search_top_k
          }]
        }
      }]
    }
  }
  response_export_values = local.agent_response_exports
  retry                  = local.permission_retry

  timeouts {
    create = "30m"
    update = "30m"
  }
}

# Second prompt registration: identical instructions/model/search tool, but
# reasoning effort fixed to "none" for latency-critical/no-reasoning comparison.
resource "azapi_data_plane_resource" "prompt_none" {
  type      = "Microsoft.Foundry/agents@v1"
  name      = var.prompt_agent_name_none
  parent_id = local.project_host

  body = {
    name = var.prompt_agent_name_none
    definition = {
      kind         = "prompt"
      model        = azapi_resource.model["prompt_none"].name
      instructions = var.agent_config.instructions
      reasoning    = { effort = "none" }
      tools = [{
        type = "azure_ai_search"
        azure_ai_search = {
          indexes = [{
            project_connection_id = var.search_project_connection_id
            index_name            = var.search_index_name
            query_type            = var.agent_config.search_query_type
            top_k                 = var.agent_config.search_top_k
          }]
        }
      }]
    }
  }
  response_export_values = local.agent_response_exports
  retry                  = local.permission_retry

  timeouts {
    create = "30m"
    update = "30m"
  }
}

# Current contract automatically provisions on registration; there is NO start
# action. AzAPI does not wait for status=active: deployment/deploy.py verifies
# readiness separately. Its separately approved route-agents step selects the
# exact returned version on the dedicated endpoint (AzAPI lacks data-plane PATCH).
# Runtime calls the model with the SAME native Search tool,
# not an agent_reference to the prompt agent.
# https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent
resource "azapi_data_plane_resource" "hosted" {
  type      = "Microsoft.Foundry/agents@v1"
  name      = var.hosted_agent_name
  parent_id = local.project_host

  body = {
    name = var.hosted_agent_name
    definition = {
      kind                    = "hosted"
      container_configuration = { image = var.hosted_image }
      cpu                     = "1"
      memory                  = "2Gi"
      # Current container contract: runtime forwards the opaque per-request
      # x-agent-foundry-call-id unchanged on downstream Foundry requests.
      # https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract#platform-request-headers-container-protocol-200
      protocol_versions = [{ protocol = "responses", version = "2.0.0" }]
      # FOUNDRY_PROJECT_ENDPOINT and APPLICATIONINSIGHTS_CONNECTION_STRING are
      # platform-injected. Never override reserved FOUNDRY_* or set AZURE_CLIENT_ID
      # to the project's MI: the sandbox has a separate platform agent identity.
      environment_variables = merge(local.runtime_environment, {
        REASONING_EFFORT_OVERRIDE = "low"
        MODEL_DEPLOYMENT_NAME     = azapi_resource.model["hosted"].name
      })
    }
  }
  response_export_values = local.agent_response_exports
  retry                  = local.permission_retry

  lifecycle {
    precondition {
      condition     = startswith(var.hosted_image, "${var.registry_login_server}/")
      error_message = "The hosted image must be a real digest in this foundation's shared ACR."
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
  }
}

# Same image, same infrastructure: only the reasoning override differs. This
# keeps one built/pushed hosted image serving both hosted registrations.
resource "azapi_data_plane_resource" "hosted_none" {
  type      = "Microsoft.Foundry/agents@v1"
  name      = var.hosted_agent_name_none
  parent_id = local.project_host

  body = {
    name = var.hosted_agent_name_none
    definition = {
      kind                    = "hosted"
      container_configuration = { image = var.hosted_image }
      cpu                     = "1"
      memory                  = "2Gi"
      protocol_versions       = [{ protocol = "responses", version = "2.0.0" }]
      environment_variables = merge(local.runtime_environment, {
        REASONING_EFFORT_OVERRIDE = "none"
        MODEL_DEPLOYMENT_NAME     = azapi_resource.model["hosted_none"].name
      })
    }
  }
  response_export_values = local.agent_response_exports
  retry                  = local.permission_retry

  lifecycle {
    precondition {
      condition     = startswith(var.hosted_image, "${var.registry_login_server}/")
      error_message = "The hosted image must be a real digest in this foundation's shared ACR."
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
  }
}

resource "azapi_resource" "web" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-${var.name_token}"
  parent_id = var.resource_group_id
  location  = var.location
  tags      = var.tags

  # Azure reorders probes. Match by type so missing fields cannot migrate from
  # Startup to Liveness during refresh; nested list paths omit array indexes.
  list_unique_id_property = {
    "properties.template.containers.probes" = "type"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [var.ui_identity_id]
  }

  body = {
    properties = {
      environmentId       = var.container_apps_environment_id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external   = true
          targetPort = 8080
          # Match Azure's returned spelling without ignoring case across env/secrets.
          transport     = "Auto"
          allowInsecure = false
          traffic       = [{ latestRevision = true, weight = 100 }]
        }
        registries = [{
          server   = var.registry_login_server
          identity = var.ui_identity_id
        }]
        secrets = [{
          name  = "application-insights"
          value = var.application_insights_connection_string
        }]
      }
      template = {
        containers = [{
          name      = "web"
          image     = var.web_image
          resources = { cpu = 0.5, memory = "1Gi" }
          env = concat(
            [for key, value in merge(local.runtime_environment, {
              AZURE_CLIENT_ID          = var.ui_identity_client_id
              FOUNDRY_PROJECT_ENDPOINT = var.project_endpoint
              PROMPT_AGENT_NAME_NONE   = var.prompt_agent_name_none
              HOSTED_AGENT_NAME_NONE   = var.hosted_agent_name_none
              # Validation/display only: routing for prompt/hosted is fixed at
              # Terraform registration/per-resource env override, each using its
              # OWN dedicated deployment (see azapi_resource.model above).
              MODEL_DEPLOYMENT_NAME = azapi_resource.model["prompt"].name
              # Expected versions for runtime diagnostics. Actual dedicated
              # endpoint selection is managed by the approved route-agents step.
              HOSTED_AGENT_VERSION      = tostring(azapi_data_plane_resource.hosted.output.agent_version)
              PROMPT_AGENT_VERSION      = tostring(azapi_data_plane_resource.prompt.output.agent_version)
              HOSTED_NONE_AGENT_VERSION = tostring(azapi_data_plane_resource.hosted_none.output.agent_version)
              PROMPT_NONE_AGENT_VERSION = tostring(azapi_data_plane_resource.prompt_none.output.agent_version)
              # Refreshed on every apply so the UI footer can confirm a redeploy
              # actually reached this container, instead of guessing from cache.
              DEPLOYED_AT = timestamp()
            }) : { name = key, value = value }],
            [{ name = "APPLICATIONINSIGHTS_CONNECTION_STRING", secretRef = "application-insights" }]
          )
          probes = [
            {
              type                = "Startup"
              httpGet             = { path = "/health", port = 8080, scheme = "HTTP" }
              periodSeconds       = 5
              failureThreshold    = 30
              initialDelaySeconds = 5
              timeoutSeconds      = 3
            },
            {
              type             = "Readiness"
              httpGet          = { path = "/health", port = 8080, scheme = "HTTP" }
              periodSeconds    = 10
              failureThreshold = 3
              timeoutSeconds   = 3
            },
            {
              type             = "Liveness"
              httpGet          = { path = "/health", port = 8080, scheme = "HTTP" }
              periodSeconds    = 30
              failureThreshold = 3
              timeoutSeconds   = 3
            },
          ]
        }]
        # Current UI sessions/limits are process-local; scale-out would fragment
        # comparisons. One replica/worker is deliberate until shared state exists.
        scale = { minReplicas = 1, maxReplicas = 1 }
      }
    }
  }
  response_export_values = ["properties.configuration.ingress.fqdn"]
  retry                  = local.permission_retry

  lifecycle {
    precondition {
      condition     = startswith(var.web_image, "${var.registry_login_server}/")
      error_message = "The UI image must be a real digest in this foundation's shared ACR."
    }
  }
}
