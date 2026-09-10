variable "deploy_workloads" {
  type        = bool
  default     = false
  nullable    = false
  description = "False bootstraps services and permissions only; true creates the Search index, registers both agents and deploys the UI after authorization readiness checks. Never switch back to false to update foundation."
}

variable "environment" {
  type        = string
  default     = "dev"
  nullable    = false
  description = "Environment suffix used in names and tags. The checked-in local backend is development bootstrap only."

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Use dev, test, or prod; configure a remote backend before shared or production use."
  }
}

variable "hosted_agent_name" {
  type        = string
  default     = "search-hosted"
  nullable    = false
  description = "Registered hosted agent name (low reasoning); must differ from all other registered agent names."

  validation {
    condition = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", var.hosted_agent_name)) && length(distinct([
      var.hosted_agent_name, var.prompt_agent_name, var.hosted_agent_name_none, var.prompt_agent_name_none
    ])) == 4
    error_message = "Use a distinct 1-63 character alphanumeric name with internal hyphens only."
  }
}

variable "hosted_agent_name_none" {
  type        = string
  default     = "search-hosted-none"
  nullable    = false
  description = "Registered hosted agent name (no reasoning); must differ from all other registered agent names."

  validation {
    condition     = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", var.hosted_agent_name_none))
    error_message = "Use a 1-63 character alphanumeric name with internal hyphens only."
  }
}

variable "hosted_image" {
  type        = string
  default     = null
  description = "Real linux/amd64 hosted image in the shared ACR, pinned as registry/repository@sha256:digest. Required when deploying workloads."

  validation {
    condition     = var.hosted_image == null ? !var.deploy_workloads : can(regex("^[a-z0-9]+\\.azurecr\\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$", var.hosted_image))
    error_message = "Supply an immutable ACR hosted image digest when deploy_workloads=true."
  }
}

variable "location" {
  type        = string
  default     = "swedencentral"
  nullable    = false
  description = "Azure public-cloud region. Verify model availability and quota before changing it."

  validation {
    condition     = can(regex("^[a-z]+[a-z0-9]*$", var.location))
    error_message = "Use a lowercase Azure location identifier such as swedencentral."
  }
}

variable "model_capacity" {
  type        = number
  default     = 10
  nullable    = false
  description = "GlobalStandard model capacity units, not a universal tokens-per-minute value; subject to subscription quota."

  validation {
    condition     = var.model_capacity >= 1 && var.model_capacity <= 1000 && floor(var.model_capacity) == var.model_capacity
    error_message = "Model capacity must be an integer from 1 to 1000; verify available quota separately."
  }
}

variable "model_deployment_name" {
  type        = string
  default     = "gpt-5.6-terra"
  nullable    = false
  description = "Legacy single account-level model deployment name/capacity kept in the foundry module for foundation-stage stability; no agent uses it anymore (each agent has its own dedicated deployment via model_deployments). Changing it requires a foundation-stage apply."

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", var.model_deployment_name))
    error_message = "Use 1-64 letters, numbers, dots, underscores or hyphens, beginning with a letter or number."
  }
}

variable "model_deployments" {
  type = map(object({
    name     = string
    capacity = number
  }))
  nullable    = false
  description = "Dedicated per-agent-side GlobalStandard model deployment name/capacity, keyed by prompt/prompt_none/hosted/hosted_none. Isolates each agent's TPM/RPM budget so concurrent calls from the other agents cannot skew one agent's latency; all four must have identical capacity so the comparison stays fair."

  validation {
    condition = (
      toset(keys(var.model_deployments)) == toset(["prompt", "prompt_none", "hosted", "hosted_none"]) &&
      length(distinct([for d in values(var.model_deployments) : d.name])) == 4 &&
      length(distinct([for d in values(var.model_deployments) : d.capacity])) == 1 &&
      alltrue([for d in values(var.model_deployments) : d.capacity >= 1 && d.capacity <= 1000 && floor(d.capacity) == d.capacity])
    )
    error_message = "Supply exactly prompt/prompt_none/hosted/hosted_none entries with four distinct names and one identical integer capacity (1-1000)."
  }
}

variable "model_name" {
  type        = string
  default     = "gpt-5.6-terra"
  nullable    = false
  description = "OpenAI model catalog name; the default was verified read-only by the operator for the target subscription."

  validation {
    condition     = length(trimspace(var.model_name)) > 0 && var.model_name == trimspace(var.model_name)
    error_message = "Model name must be nonempty with no surrounding whitespace."
  }
}

variable "model_version" {
  type        = string
  default     = "2026-07-09"
  nullable    = false
  description = "Explicit catalog version, shared by both agents. Automatic version upgrades are disabled."

  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-\\d{2}$", var.model_version))
    error_message = "Supply the verified model version in YYYY-MM-DD format."
  }
}

variable "name_prefix" {
  type        = string
  default     = "fsearch"
  nullable    = false
  description = "Short application prefix; combined with environment and a stable subscription-derived hash for global names."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.name_prefix))
    error_message = "Use 3-12 lowercase letters or digits, starting with a letter."
  }
}

variable "prompt_agent_name" {
  type        = string
  default     = "search-prompt"
  nullable    = false
  description = "Registered native Search prompt agent name (low reasoning)."

  validation {
    condition     = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", var.prompt_agent_name))
    error_message = "Use a 1-63 character alphanumeric name with internal hyphens only."
  }
}

variable "prompt_agent_name_none" {
  type        = string
  default     = "search-prompt-none"
  nullable    = false
  description = "Registered native Search prompt agent name (no reasoning)."

  validation {
    condition     = can(regex("^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$", var.prompt_agent_name_none))
    error_message = "Use a 1-63 character alphanumeric name with internal hyphens only."
  }
}

variable "search_index_name" {
  type        = string
  default     = "public-documents"
  nullable    = false
  description = "Lexical index with id/title/content/url fields, created in the workload stage after authorization readiness. Upload only public demo documents manually."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$", var.search_index_name)) && !strcontains(var.search_index_name, "--")
    error_message = "Use 2-128 lowercase letters, digits or single hyphens; start and end with a letter or digit."
  }
}

variable "search_operator" {
  type = object({
    principal_id   = string
    principal_type = optional(string, "User")
  })
  default     = null
  description = "Optional uploader/index administrator override (Entra object ID, not client ID). Null grants document upload to the current Terraform identity; use public documents only."

  validation {
    condition = var.search_operator == null ? true : (
      can(regex("(?i)^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", var.search_operator.principal_id)) &&
      contains(["User", "Group", "ServicePrincipal"], var.search_operator.principal_type)
    )
    error_message = "Supply a GUID principal_id and a principal_type of User, Group, or ServicePrincipal."
  }
}

variable "search_sku" {
  type        = string
  default     = "basic"
  nullable    = false
  description = "Azure AI Search tier: basic or standard (Standard S1). Standard S1 has a higher hourly capacity cost."

  validation {
    condition     = contains(["basic", "standard"], var.search_sku)
    error_message = "Search SKU must be basic or standard (Standard S1)."
  }
}

variable "subscription_id" {
  type        = string
  nullable    = false
  description = "Target Azure subscription UUID. Supply privately with TF_VAR_subscription_id or an ignored environment tfvars file."

  validation {
    condition     = can(regex("(?i)^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", var.subscription_id)) && var.subscription_id != "00000000-0000-0000-0000-000000000000"
    error_message = "Supply a real subscription UUID, not the example placeholder."
  }
}

variable "tags" {
  type        = map(string)
  default     = {}
  nullable    = false
  description = "Additional non-secret resource tags; root environment/managed_by/project tags take precedence."

  validation {
    condition     = length(var.tags) <= 47 && alltrue([for key, value in var.tags : length(key) > 0 && length(key) <= 128 && (value == null ? false : length(value) <= 256)])
    error_message = "Supply at most 47 tags with 1-128 character keys and non-null values up to 256 characters."
  }
}

variable "tenant_id" {
  type        = string
  nullable    = false
  description = "Target Microsoft Entra tenant UUID. Supply privately with TF_VAR_tenant_id or an ignored environment tfvars file."

  validation {
    condition     = can(regex("(?i)^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", var.tenant_id)) && var.tenant_id != "00000000-0000-0000-0000-000000000000"
    error_message = "Supply a real tenant UUID, not the example placeholder."
  }

}

variable "web_image" {
  type        = string
  default     = null
  description = "Real linux/amd64 UI image in the shared ACR, pinned as registry/repository@sha256:digest. Required when deploying workloads."

  validation {
    condition     = var.web_image == null ? !var.deploy_workloads : can(regex("^[a-z0-9]+\\.azurecr\\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$", var.web_image))
    error_message = "Supply an immutable ACR UI image digest when deploy_workloads=true."
  }
}
