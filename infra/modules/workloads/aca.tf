locals {
  aca_sides           = { aca = "low", aca_none = "none" }
  aca_bootstrap_image = "mcr.microsoft.com/k8se/quickstart@sha256:3a4d93c34c6753f24765ab17a36f1754aee9b02082b7d9553d17697b9e8252c4"
  aca_assignments = merge([
    for side in keys(local.aca_sides) : {
      "${side}_acr_pull" = {
        principal_id = azapi_resource.aca[side].identity[0].principal_id
        scope        = var.registry_id
        role_id      = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
      }
      "${side}_foundry" = {
        principal_id = azapi_resource.aca[side].identity[0].principal_id
        scope        = var.project_id
        role_id      = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
      }
    }
  ]...)
}

resource "azapi_resource" "aca" {
  for_each = local.aca_sides

  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-${each.key == "aca" ? "aca" : "acan"}-${var.name_token}"
  parent_id = var.resource_group_id
  location  = var.location
  tags      = var.tags

  identity {
    type = "SystemAssigned"
  }

  list_unique_id_property = {
    "properties.template.containers.probes" = "type"
  }

  body = {
    properties = {
      environmentId       = var.container_apps_environment_id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        identitySettings    = [{ identity = "system", lifecycle = var.aca_bootstrap ? "None" : "Main" }]
        ingress = {
          external      = false
          targetPort    = var.aca_bootstrap ? 80 : 8088
          transport     = "Auto"
          allowInsecure = false
          traffic       = [{ latestRevision = true, weight = 100 }]
        }
        registries = var.aca_bootstrap ? [] : [{ server = var.registry_login_server, identity = "system" }]
        secrets    = var.aca_bootstrap ? [] : [{ name = "application-insights", value = var.application_insights_connection_string }]
      }
      template = {
        containers = [{
          name      = "agent"
          image     = var.aca_bootstrap ? local.aca_bootstrap_image : var.hosted_image
          resources = { cpu = 1, memory = "2Gi" }
          env = var.aca_bootstrap ? [] : concat(
            [for key, value in merge(local.runtime_environment, {
              FOUNDRY_PROJECT_ENDPOINT  = var.project_endpoint
              MODEL_DEPLOYMENT_NAME     = azapi_resource.model[each.key].name
              REASONING_EFFORT_OVERRIDE = each.value
              RUNTIME_SIDE              = each.key
              MANAGED_IDENTITY_MODE     = "system"
            }) : { name = key, value = value }],
            [{ name = "APPLICATIONINSIGHTS_CONNECTION_STRING", secretRef = "application-insights" }]
          )
          probes = var.aca_bootstrap ? [] : [for probe in ["Startup", "Readiness", "Liveness"] : {
            type             = probe
            httpGet          = { path = "/health", port = 8088, scheme = "HTTP" }
            periodSeconds    = 10
            failureThreshold = probe == "Startup" ? 30 : 3
            timeoutSeconds   = 3
          }]
        }]
        scale = { minReplicas = var.aca_bootstrap ? 0 : 1, maxReplicas = 1 }
      }
    }
  }
  response_export_values = ["properties.configuration.ingress.fqdn", "properties.provisioningState"]
  retry                  = local.permission_retry

  lifecycle {
    precondition {
      condition     = length("ca-${each.key == "aca" ? "aca" : "acan"}-${var.name_token}") <= 32
      error_message = "Container App names must not exceed 32 characters; shorten name_token."
    }
    precondition {
      condition     = startswith(var.hosted_image, "${var.registry_login_server}/")
      error_message = "ACA runtime must reuse the approved hosted image in the shared ACR."
    }
  }
}

resource "azapi_resource" "aca_assignment" {
  for_each = local.aca_assignments

  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", lower("${each.value.scope}/${each.value.principal_id}/${each.value.role_id}"))
  parent_id = each.value.scope
  body = {
    properties = {
      principalId      = each.value.principal_id
      principalType    = "ServicePrincipal"
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value.role_id}"
    }
  }
}