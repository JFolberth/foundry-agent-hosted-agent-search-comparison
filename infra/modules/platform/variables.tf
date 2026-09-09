variable "location" {
  type        = string
  nullable    = false
  description = "Azure public-cloud region."
}

variable "name_token" {
  type        = string
  nullable    = false
  description = "Validated root naming token containing prefix, environment and stable hash."
}

variable "subscription_id" {
  type        = string
  nullable    = false
  description = "Subscription UUID owning the resource group."
}

variable "tags" {
  type        = map(string)
  nullable    = false
  description = "Shared non-secret resource tags."
}
