# Explicit single-operator DEVELOPMENT bootstrap only; no existing state is migrated.
# State contains the App Insights connection string. Keep it on an encrypted disk,
# restrict filesystem access, and never upload state or .terraform/ to GitHub.
#
# Before sharing this deployment or using production infrastructure, provision an
# Azure Storage state account/container in a separately approved bootstrap lifecycle,
# enable encryption and blob versioning, and grant the Terraform identity Storage
# Blob Data Contributor on the container. Backend storage must outlive this root.
#
# Replace the local block below with: backend "azurerm" {}
# Copy backend.hcl.example to ignored backend.hcl and fill in its values.
# From infra/, initialize the new backend explicitly:
#   terraform init -backend-config=backend.hcl
# If local state already exists, migration requires separate approval and
#   terraform init -migrate-state -backend-config=backend.hcl
# Never use -migrate-state for an empty initial deployment.
#
# "azurerm" here is Terraform's built-in Azure Blob BACKEND, not a provider.
# The only resource provider remains Azure/azapi.
# https://developer.hashicorp.com/terraform/language/backend/azurerm
terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
