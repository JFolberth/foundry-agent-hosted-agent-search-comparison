variable "account_id" {
  type        = string
  nullable    = false
  description = "Foundry AIServices account ARM ID; parent for the four dedicated per-agent model deployments."
}

variable "agent_config" {
  type = object({
    instructions      = string
    reasoning_effort  = string
    search_query_type = string
    search_top_k      = number
    max_output_tokens = number
  })
  nullable    = false
  description = "Canonical config/agent.json, also baked into both immutable images. max_output_tokens is a Responses-request setting, not a prompt definition field."

  validation {
    condition = (
      length(trimspace(var.agent_config.instructions)) > 0 &&
      length(var.agent_config.instructions) <= 16000 &&
      var.agent_config.reasoning_effort == "low" &&
      var.agent_config.search_query_type == "simple" &&
      var.agent_config.search_top_k == 5 &&
      var.agent_config.max_output_tokens == 4096
    )
    error_message = "Canonical config must have nonempty instructions (<=16000 chars), low reasoning, simple search, top_k=5, and max_output_tokens=4096."
  }
}

variable "application_insights_connection_string" {
  type        = string
  nullable    = false
  sensitive   = true
  description = "Server-only UI telemetry connection string stored as a Container Apps secret."
}

variable "container_apps_environment_id" {
  type        = string
  nullable    = false
  description = "Existing foundation-managed Container Apps environment ARM ID."
}

variable "hosted_agent_name" {
  type        = string
  nullable    = false
  description = "Registered hosted agent name (low reasoning)."
}

variable "hosted_agent_name_none" {
  type        = string
  nullable    = false
  description = "Registered hosted agent name (no reasoning); reuses the same hosted_image with REASONING_EFFORT_OVERRIDE=none."
}

variable "hosted_image" {
  type        = string
  nullable    = false
  description = "Real linux/amd64 hosted image pinned by digest in the shared registry."
}

variable "location" {
  type        = string
  nullable    = false
  description = "Azure region for the UI Container App."
}

variable "model_deployments" {
  type = map(object({
    name     = string
    capacity = number
  }))
  nullable    = false
  description = "Dedicated per-agent-side GlobalStandard model deployment name/capacity, keyed by prompt/prompt_none/hosted/hosted_none. Each agent gets its own TPM/RPM budget so one agent's concurrent load cannot skew another's latency; all four must share model_name/model_version and have identical capacity so the comparison stays fair."

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
  nullable    = false
  description = "Verified OpenAI model catalog name, shared by all four dedicated deployments."
}

variable "model_version" {
  type        = string
  nullable    = false
  description = "Explicit verified OpenAI model catalog version, shared by all four dedicated deployments."
}

variable "name_token" {
  type        = string
  nullable    = false
  description = "Root's validated resource naming token."
}

variable "project_endpoint" {
  type        = string
  nullable    = false
  description = "Service-returned Foundry project endpoint."
}

variable "prompt_agent_name" {
  type        = string
  nullable    = false
  description = "Registered prompt agent name (low reasoning), distinct from the hosted agent."
}

variable "prompt_agent_name_none" {
  type        = string
  nullable    = false
  description = "Registered prompt agent name (no reasoning), distinct from all other registered agent names."
}

variable "registry_login_server" {
  type        = string
  nullable    = false
  description = "Shared ACR hostname; both image digests must use this registry."
}

variable "resource_group_id" {
  type        = string
  nullable    = false
  description = "Foundation resource group ARM ID."
}

variable "search_index_name" {
  type        = string
  nullable    = false
  description = "Shared Terraform-managed lexical index name."
}

variable "search_project_connection_id" {
  type        = string
  nullable    = false
  description = "Shared AAD native Search project connection ARM ID."
}

variable "tags" {
  type        = map(string)
  nullable    = false
  description = "Shared resource tags."
}

variable "ui_identity_client_id" {
  type        = string
  nullable    = false
  description = "UI backend managed identity client ID."
}

variable "ui_identity_id" {
  type        = string
  nullable    = false
  description = "UI user-assigned identity ARM ID; used for ACR pull and backend credentials."
}

variable "web_image" {
  type        = string
  nullable    = false
  description = "Real linux/amd64 UI image pinned by digest in the shared registry."
}
