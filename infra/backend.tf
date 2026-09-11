# Remote backend: Azure Storage (saterraformstatedevswc / rg-terraformstate-dev /
# container "tfstate"). State contains the App Insights connection string; access
# to the storage account/container is restricted via RBAC (Storage Blob Data
# Contributor), not filesystem permissions.
#
# infra/backend.hcl is gitignored and holds the actual (non-secret) backend
# config values; copy backend.hcl.example if you need to recreate it locally.
# From infra/, initialize explicitly:
#   terraform init -backend-config=backend.hcl
# Migrating existing local state into this backend (one-time, already done for
# this repo) requires separate approval and:
#   terraform init -migrate-state -backend-config=backend.hcl
#
# "azurerm" here is Terraform's built-in Azure Blob BACKEND, not a provider.
# The only resource provider remains Azure/azapi.
# https://developer.hashicorp.com/terraform/language/backend/azurerm
terraform {
  backend "azurerm" {}
}
