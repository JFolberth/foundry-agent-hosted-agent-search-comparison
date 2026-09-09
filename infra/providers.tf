provider "azapi" {
  subscription_id        = var.subscription_id
  tenant_id              = var.tenant_id
  default_tags           = local.tags
  disable_default_output = true
  # Planning must not implicitly mutate subscription-level provider registration.
  # Required namespaces must be registered separately with explicit approval.
  skip_provider_registration = true
}

data "azapi_client_config" "current" {}
