variable "application_insights_connection_string" {
  type        = string
  nullable    = false
  sensitive   = true
  description = "Workspace-based App Insights connection string for the project's telemetry connection."
}

variable "application_insights_id" {
  type        = string
  nullable    = false
  description = "App Insights ARM ID, not workspace ID or ingestion endpoint."
}

variable "location" {
  type        = string
  nullable    = false
  description = "Azure region with verified model availability."
}

variable "model_capacity" {
  type        = number
  nullable    = false
  description = "GlobalStandard model deployment capacity units."
}

variable "model_deployment_name" {
  type        = string
  nullable    = false
  description = "Single model deployment name shared by both future agents."
}

variable "model_name" {
  type        = string
  nullable    = false
  description = "Verified OpenAI model catalog name."
}

variable "model_version" {
  type        = string
  nullable    = false
  description = "Explicit verified OpenAI model catalog version."
}

variable "name_token" {
  type        = string
  nullable    = false
  description = "Validated root naming token."
}

variable "resource_group_id" {
  type        = string
  nullable    = false
  description = "Parent resource group ARM ID."
}

variable "search_endpoint" {
  type        = string
  nullable    = false
  description = "Search service HTTPS endpoint for the native CognitiveSearch connection."
}

variable "search_id" {
  type        = string
  nullable    = false
  description = "Search service ARM ID included in connection metadata."
}

variable "tags" {
  type        = map(string)
  nullable    = false
  description = "Shared non-secret resource tags."
}
