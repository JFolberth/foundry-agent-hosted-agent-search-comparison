# Copy to an ignored environment file; replace placeholders before deployment.
# From infra/: terraform init -backend=false && terraform validate
# Planning/deployment require separate approval. No plan is run during validation.
# Set subscription/tenant via TF_VAR_* instead of this file if preferred.
# Foundation creates services/permissions, not the index or agents. The deployment
# helper must pass authorization GETs before workload plan AND apply; see main.tf.
deploy_workloads       = false
environment            = "dev"
hosted_agent_name      = "search-hosted"
hosted_agent_name_none = "search-hosted-none"
hosted_image           = null
location               = "swedencentral"
model_capacity         = 460 # Legacy foundation deployment; orphaned/unused, kept only to avoid a foundation-stage change.
model_deployment_name  = "gpt-5.6-terra"
model_deployments = { # Dedicated per-agent deployments (equal capacity keeps the comparison fair); 4x80=320 fits comfortably under the swedencentral GlobalStandard quota headroom.
  prompt      = { name = "gpt-5.6-terra-prompt", capacity = 80 }
  prompt_none = { name = "gpt-5.6-terra-prompt-none", capacity = 80 }
  hosted      = { name = "gpt-5.6-terra-hosted", capacity = 80 }
  hosted_none = { name = "gpt-5.6-terra-hosted-none", capacity = 80 }
}
model_name             = "gpt-5.6-terra"
model_version          = "2026-07-09"
name_prefix            = "fsearch"
prompt_agent_name      = "search-prompt"
prompt_agent_name_none = "search-prompt-none"
search_index_name      = "public-documents"
search_operator        = null
search_sku             = "standard" # S1 recovery attempt; higher hourly cost than Basic.
subscription_id        = "00000000-0000-0000-0000-000000000000"
tags                   = {}
tenant_id              = "00000000-0000-0000-0000-000000000000"
web_image              = null
