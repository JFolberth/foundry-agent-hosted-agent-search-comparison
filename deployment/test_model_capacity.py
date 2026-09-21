import copy

import pytest

import deploy
from test_deploy import WorkloadPlanTests


def capacity_plan():
    deployments = {side: {"name": side, "capacity": 64} for side in deploy.MODEL_SIDES}
    changes = []
    for side in deploy.AGENT_SIDES:
        before = {
            "type": "Microsoft.CognitiveServices/accounts/deployments@2026-03-01",
            "name": side, "parent_id": "unchanged-account",
            "body": {"sku": {"name": "GlobalStandard", "capacity": 80},
                     "properties": {"model": {"name": "gpt-5.6-terra", "version": "2026-07-09"}}},
        }
        after = copy.deepcopy(before)
        after["body"]["sku"]["capacity"] = 64
        changes.append({"address": f'module.workloads[0].azapi_resource.model["{side}"]',
                        "mode": "managed", "change": {"actions": ["update"], "before": before, "after": after}})
    return {"variables": {"model_deployments": {"value": deployments}}, "resource_changes": changes}


def test_capacity_only_reduction_is_allowed():
    deploy.model_capacity_guard(capacity_plan())


def test_targeted_capacity_plan_uses_prior_workload_flag():
    session, plan = WorkloadPlanTests().plan()
    capacity = capacity_plan()
    plan["variables"].update(capacity["variables"])
    plan["resource_changes"] = capacity["resource_changes"]
    plan["planned_values"]["outputs"].pop("deploy_workloads")
    deploy.validate_plan(session, plan, "model-capacity", {"hosted_image": "approved", "web_image": "approved"})
    plan["prior_state"]["values"]["outputs"]["deploy_workloads"]["value"] = False
    with pytest.raises(deploy.DeploymentError):
        deploy.validate_plan(session, plan, "model-capacity", {"hosted_image": "approved", "web_image": "approved"})


@pytest.mark.parametrize("mutation", ["version", "scope", "increase", "unequal", "create", "missing", "other"])
def test_capacity_stage_rejects_other_changes(mutation):
    plan = capacity_plan()
    resource = plan["resource_changes"][0]
    after = resource["change"]["after"]
    if mutation == "version":
        after["body"]["properties"]["model"]["version"] = "other"
    elif mutation == "scope":
        after["parent_id"] = "other-account"
    elif mutation == "increase":
        after["body"]["sku"]["capacity"] = 90
    elif mutation == "unequal":
        plan["variables"]["model_deployments"]["value"]["aca"]["capacity"] = 65
    elif mutation == "create":
        resource["change"]["actions"] = ["create"]
    elif mutation == "missing":
        plan["resource_changes"].pop()
    else:
        resource["address"] = "module.workloads[0].azapi_resource.web"
    with pytest.raises(deploy.DeploymentError):
        deploy.model_capacity_guard(plan)