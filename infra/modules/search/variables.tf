variable "create_index" {
  type        = bool
  default     = false
  nullable    = false
  description = "Create the managed data-plane index only after foundation RBAC and the deployment helper's authorization GET gate succeed."
}

variable "execution_principal_id" {
  type        = string
  nullable    = false
  description = "Current Terraform identity's Entra object ID; granted index-definition management, not document access."
}

variable "index_name" {
  type        = string
  nullable    = false
  description = "Validated lexical index name."
}

variable "location" {
  type        = string
  nullable    = false
  description = "Azure public-cloud region."
}

variable "name" {
  type        = string
  nullable    = false
  description = "Globally unique Search service name."
}

variable "operator" {
  type = object({
    principal_id   = string
    principal_type = string
  })
  default     = null
  description = "Optional document uploader/index administrator override; null uses the execution identity."
}

variable "resource_group_id" {
  type        = string
  nullable    = false
  description = "Parent resource group ARM ID."
}

variable "sku" {
  type        = string
  default     = "basic"
  nullable    = false
  description = "Search service SKU; standard is Standard S1, not S2 or S3."

  validation {
    condition     = contains(["basic", "standard"], var.sku)
    error_message = "Search SKU must be basic or standard (Standard S1)."
  }
}

variable "subscription_id" {
  type        = string
  nullable    = false
  description = "Subscription UUID used for built-in role definition IDs."
}

variable "tags" {
  type        = map(string)
  nullable    = false
  description = "Shared non-secret resource tags."
}
