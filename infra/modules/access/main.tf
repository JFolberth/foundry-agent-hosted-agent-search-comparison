resource "azapi_resource" "assignment" {
  for_each = local.assignments

  type      = "Microsoft.Authorization/roleAssignments@2022-04-01"
  name      = uuidv5("url", lower("${each.value.scope}/${each.value.principal_id}/${each.value.role_id}"))
  parent_id = each.value.scope

  body = {
    properties = merge({
      principalId      = each.value.principal_id
      roleDefinitionId = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value.role_id}"
    }, startswith(each.key, "execution_") ? {} : { principalType = "ServicePrincipal" })
  }
}
