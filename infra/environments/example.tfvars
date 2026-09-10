# Copy to an ignored environment file; replace placeholders before deployment.
# From infra/: terraform init -backend=false && terraform validate
# Planning/deployment require separate approval. No plan is run during validation.
# Set subscription/tenant via TF_VAR_* instead of this file if preferred.
# Foundation creates services/permissions, not the index or agents. The deployment
# helper must pass authorization GETs before workload plan AND apply; see main.tf.
deploy_workloads      = false
environment           = "dev"
hosted_agent_name     = "search-hosted"
hosted_image          = null
location              = "swedencentral"
model_capacity        = 460 # Matches the live deployment; raised manually in Azure for throughput.
model_deployment_name = "gpt-5.6-terra"
model_name            = "gpt-5.6-terra"
model_version         = "2026-07-09"
name_prefix           = "fsearch"
prompt_agent_name     = "search-prompt"
search_index_name     = "public-documents"
search_operator       = null
search_sku            = "standard" # S1 recovery attempt; higher hourly cost than Basic.
subscription_id       = "00000000-0000-0000-0000-000000000000"
tags                  = {}
tenant_id             = "00000000-0000-0000-0000-000000000000"
web_image             = null
