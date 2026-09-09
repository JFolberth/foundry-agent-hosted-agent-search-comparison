locals {
  # https://learn.microsoft.com/azure/search/search-security-rbac
  index_contributor_role  = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
  service_manager_role    = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
  operator_principal_id   = var.operator == null ? var.execution_principal_id : var.operator.principal_id
  operator_principal_type = var.operator == null ? null : var.operator.principal_type
  operator_roles = merge(
    { documents = local.index_contributor_role },
    lower(local.operator_principal_id) == lower(var.execution_principal_id) ? {} : { schema = local.service_manager_role }
  )
}
