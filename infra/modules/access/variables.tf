variable "account_id" {
  type        = string
  nullable    = false
  description = "Foundry account ARM ID for project inference authorization."
}

variable "account_principal_id" {
  type        = string
  nullable    = false
  description = "Foundry account system-assigned identity object ID."
}

variable "execution_principal_id" {
  type        = string
  nullable    = false
  description = "Current Terraform identity object ID; receives project-scoped agent authoring and registry-scoped image push only."
}

variable "project_id" {
  type        = string
  nullable    = false
  description = "Project ARM ID for UI agent invocation authorization."
}

variable "project_principal_id" {
  type        = string
  nullable    = false
  description = "Foundry project system-assigned identity object ID."
}

variable "registry_id" {
  type        = string
  nullable    = false
  description = "Legacy-RBAC registry ARM ID."
}

variable "search_id" {
  type        = string
  nullable    = false
  description = "Search service ARM ID for the documented native-tool account identity roles."
}

variable "subscription_id" {
  type        = string
  nullable    = false
  description = "Subscription UUID used to qualify built-in role definitions."
}

variable "ui_principal_id" {
  type        = string
  nullable    = false
  description = "UI backend managed identity object ID."
}
