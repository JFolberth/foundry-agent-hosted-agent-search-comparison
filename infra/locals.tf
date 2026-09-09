locals {
  agent_config = jsondecode(file("${path.root}/../config/agent.json"))

  # Stable across runs without a random provider; changes to these inputs rename resources.
  suffix     = substr(sha256("${lower(var.subscription_id)}/${var.name_prefix}/${var.environment}"), 0, 10)
  name_token = "${var.name_prefix}-${var.environment}-${local.suffix}"
  tags = merge(var.tags, {
    environment = var.environment
    managed_by  = "terraform"
    project     = "foundry-agent-search-comparison"
  })
}
