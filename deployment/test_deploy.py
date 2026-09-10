import argparse
import copy
import io
import os
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import Mock, patch

import deploy


class TemporaryDirectoryTests(unittest.TestCase):
    def session(self, environment):
        arguments = argparse.Namespace(environment="dev", tfvars="example.tfvars")
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(deploy, "private_id", return_value="test-target"),
            patch.object(deploy, "input_path", return_value=Path("example.tfvars")),
            patch.object(deploy, "private_dir", side_effect=lambda path: path),
        ):
            return deploy.Session(arguments)

    def test_preserves_operating_system_temporary_directories(self):
        temporary = {"TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"}
        session = self.session(temporary)
        for key, value in temporary.items():
            self.assertEqual(session.env[key], value)

    def test_does_not_replace_default_temp_with_long_repository_path(self):
        session = self.session({})
        for key in ("TMPDIR", "TMP", "TEMP"):
            self.assertNotIn(key, session.env)
        self.assertIn("deployment", session.env["TF_DATA_DIR"])
        self.assertIn(".artifacts", session.env["TF_DATA_DIR"])


SUBSCRIPTION = "00000000-1111-2222-3333-444444444444"
PROJECT = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg"
           "/providers/Microsoft.CognitiveServices/accounts/example-foundry/projects/example-project")
ENDPOINT = "https://example-foundry.services.ai.azure.com/api/projects/example-project"
SEARCH = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg"
          "/providers/Microsoft.Search/searchServices/example-search")


def fixture():
    target = {"subscription_id": SUBSCRIPTION, "tenant_id": SUBSCRIPTION, "environment": "dev"}
    outputs = {
        "project_id": PROJECT, "project_endpoint": ENDPOINT, "deploy_workloads": True,
        "prompt_agent_name": "search-prompt", "prompt_agent_version": "1",
        "hosted_agent_name": "search-hosted", "hosted_agent_version": "2",
        "prompt_none_agent_name": "search-prompt-none", "prompt_none_agent_version": "1",
        "hosted_none_agent_name": "search-hosted-none", "hosted_none_agent_version": "2",
        "workload_agent_names": {
            "prompt": "search-prompt", "hosted": "search-hosted",
            "prompt_none": "search-prompt-none", "hosted_none": "search-hosted-none",
        },
        "search_service_id": SEARCH, "search_endpoint": "https://example-search.search.windows.net",
        "search_index_name": "documents",
    }
    definitions = {
        "prompt": {
            "kind": "prompt", "model": "gpt-5-mini", "instructions": "Use only Search.",
            "reasoning": {"effort": "low"},
            "tools": [{"type": "azure_ai_search", "azure_ai_search": {"indexes": [{
                "project_connection_id": PROJECT + "/connections/native-search",
                "index_name": "documents", "query_type": "simple", "top_k": 5,
            }]}}],
        },
        "hosted": {
            "kind": "hosted", "container_configuration": {
                "image": "exampleacr.azurecr.io/search-hosted@sha256:" + "a" * 64},
            "cpu": "1", "memory": "2Gi",
            "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
            "environment_variables": {
                "MODEL_DEPLOYMENT_NAME": "gpt-5-mini", "SEARCH_INDEX_NAME": "documents",
                "SEARCH_PROJECT_CONNECTION_ID": PROJECT + "/connections/native-search",
                "HOSTED_AGENT_NAME": "search-hosted", "PROMPT_AGENT_NAME": "search-prompt",
                "REASONING_EFFORT_OVERRIDE": "low",
            },
        },
    }
    definitions["prompt_none"] = {
        **copy.deepcopy(definitions["prompt"]), "reasoning": {"effort": "none"},
    }
    definitions["hosted_none"] = {
        **copy.deepcopy(definitions["hosted"]),
        "environment_variables": {
            **definitions["hosted"]["environment_variables"], "REASONING_EFFORT_OVERRIDE": "none",
        },
    }
    resources = [
        {"address": address, "values": {"id": resource_id}, "mode": "managed"}
        for address, resource_id in (
            ("module.foundry.azapi_resource.project", PROJECT),
            ("module.search.azapi_resource.service", SEARCH),
        )
    ]
    resources += [
        {"address": f"module.workloads[0].azapi_data_plane_resource.{side}", "mode": "managed",
         "values": {"name": outputs[f"{side}_agent_name"],
                    "parent_id": ENDPOINT.removeprefix("https://"), "type": "Microsoft.Foundry/agents@v1",
                    "output": {"agent_version": outputs[f"{side}_agent_version"]},
                    "body": {"name": outputs[f"{side}_agent_name"], "definition": definition}}}
        for side, definition in definitions.items()
    ]
    values = {"outputs": {key: {"value": value} for key, value in outputs.items()},
              "root_module": {"child_modules": [{"resources": resources}]}}
    session = Mock(target=target, env={}, args=argparse.Namespace(timeout=60))
    session.state.return_value = values
    targets = {**deploy.routing_targets(outputs, target), "definitions": definitions}
    return session, outputs, values, targets


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "test", {}, None)


class BackendTests(unittest.TestCase):
    def test_actual_checked_in_backend_ignores_commented_remote_example(self):
        text = (deploy.INFRA / "backend.tf").read_text()
        self.assertIn('backend "azurerm"', text)
        self.assertEqual(deploy.active_backends(text), ["local"])
        self.assertEqual(deploy.configured_backend(), "local")

    def test_comments_strings_templates_and_heredocs_are_not_blocks(self):
        text = r'''
        # terraform { backend "azurerm" {} }
        // terraform { backend "azurerm" {} }
        /* terraform { backend "azurerm" {} } */
        locals {
          example = "terraform { backend \"azurerm\" {} }"
          template = "${format("backend \"azurerm\" {}", "unused")}"
          escaped = "$${not_a_template} // /*"
          heredoc = <<-END
            terraform { backend "azurerm" {} }
          END
        }
        terraform /* backend "azurerm" {} */ {
          backend /* example */ "local" { path = "terraform.tfstate" }
        }
        '''
        self.assertEqual(deploy.active_backends(text), ["local"])
        self.assertEqual(deploy.active_backends('terraform { backend "azurerm" {} }'), ["azurerm"])

    def test_malformed_configuration_fails_closed(self):
        for text in ('terraform { backend "local" {', '/* unfinished', '"unfinished',
                     'terraform { backend "${"azurerm"}" {} }'):
            with self.subTest(text=text), self.assertRaises(deploy.DeploymentError):
                deploy.active_backends(text)

    def test_remote_init_against_local_backend_refused_before_terraform(self):
        session = Mock(home=deploy.ARTIFACTS / "test-unused",
                       args=argparse.Namespace(environment="prod", local_development=False,
                                               backend_config="empty.hcl"))
        with patch.object(deploy, "configured_backend", return_value="local"):
            with self.assertRaisesRegex(deploy.DeploymentError, "azurerm backend"):
                deploy.initialize(session)
        session.tf.assert_not_called()

    def test_multiple_active_backends_and_existing_metadata_are_refused(self):
        self.assertEqual(deploy.active_backends(
            'terraform { backend "local" {} backend "azurerm" {} }'), ["local", "azurerm"])
        with patch.object(deploy.INFRA.__class__, "glob", return_value=[]):
            with self.assertRaisesRegex(deploy.DeploymentError, "Exactly one"):
                deploy.configured_backend()
        session = Mock(home=deploy.ARTIFACTS / "test-unused",
                       args=argparse.Namespace(environment="dev", local_development=True))
        with (patch.object(deploy, "configured_backend", return_value="local"),
              patch.object(Path, "exists", autospec=True,
                           side_effect=lambda path: path.name == "terraform.tfstate"),
              self.assertRaisesRegex(deploy.DeploymentError, "Existing state/backend metadata")):
            deploy.initialize(session)
        session.tf.assert_not_called()


class TargetTests(unittest.TestCase):
    def test_different_subscription_refused_without_cloud_request(self):
        session, outputs, _, _ = fixture()
        outputs["project_id"] = PROJECT.replace(SUBSCRIPTION, "99999999-1111-2222-3333-444444444444")
        with patch.object(deploy, "azure_request") as request:
            with self.assertRaisesRegex(deploy.DeploymentError, "requested subscription"):
                deploy.confirm_project(session, outputs)
        request.assert_not_called()

    def test_authoritative_arm_endpoint_and_id_required(self):
        session, outputs, _, _ = fixture()
        for resource_id, endpoint in (
            (PROJECT, ENDPOINT.replace("example-foundry", "another-foundry")),
            (PROJECT + "-other", ENDPOINT),
            (PROJECT, None),
        ):
            with self.subTest(endpoint=endpoint), patch.object(deploy, "azure_request", return_value={
                "id": resource_id, "properties": {"endpoints": {"AI Foundry API": endpoint}},
            }) as request:
                with self.assertRaises(deploy.DeploymentError):
                    deploy.confirm_project(session, outputs)
                self.assertEqual(request.call_args.args[1],
                                 "https://management.azure.com" + PROJECT + "?api-version=2026-03-01")
                self.assertEqual(request.call_args.args[2], "https://management.azure.com/")

    def test_matching_arm_response_accepts_resource_id_case(self):
        session, outputs, _, _ = fixture()
        with patch.object(deploy, "azure_request", return_value={
            "id": PROJECT.upper(), "properties": {"endpoints": {"AI Foundry API": ENDPOINT + "/"}},
        }):
            deploy.confirm_project(session, outputs)

    def test_malicious_project_id_or_endpoint_rejected(self):
        session, outputs, _, _ = fixture()
        for key, value in (
            ("project_id", PROJECT + "?other=1"),
            ("project_id", PROJECT + "/../other"),
            ("project_endpoint", ENDPOINT + "?redirect=1"),
            ("project_endpoint", "https://example.services.ai.azure.com.evil.invalid/api/projects/test"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(deploy.DeploymentError):
                deploy.routing_targets({**outputs, key: value}, session.target)


class DefinitionTests(unittest.TestCase):
    def test_expectations_come_from_applied_body_not_latest_or_files(self):
        session, _, values, targets = fixture()
        for resource in values["root_module"]["child_modules"][0]["resources"]:
            resource["values"].setdefault("output", {})["definition"] = {"instructions": "unapproved latest"}
        with patch.object(deploy, "infra_inputs", side_effect=AssertionError("must not read files")):
            actual, _ = deploy.applied_targets(session)
        self.assertEqual(actual, targets)
        proposal = deploy.prepare_routes(session.target, actual, {})
        changed = copy.deepcopy(proposal)
        changed["outputs"]["definitions"]["prompt"]["instructions"] = "unapproved"
        self.assertNotEqual(deploy.source_hash(proposal), deploy.source_hash(changed))

    def test_wrong_applied_resource_parent_or_name_is_rejected(self):
        for key in ("parent_id", "name"):
            session, _, values, _ = fixture()
            values["root_module"]["child_modules"][0]["resources"][-1]["values"][key] = "other"
            with self.subTest(key=key), self.assertRaises(deploy.DeploymentError):
                deploy.applied_targets(session)

    def test_unhandled_managed_state_fields_fail_closed(self):
        session, _, values, _ = fixture()
        resource = values["root_module"]["child_modules"][0]["resources"][-1]
        resource["values"]["body"]["definition"]["new_managed_setting"] = "must be reviewed"
        with self.assertRaisesRegex(deploy.DeploymentError, "unmanaged fields"):
            deploy.applied_targets(session)

    def test_root_version_must_match_registered_resource_version(self):
        session, _, values, _ = fixture()
        values["outputs"]["hosted_agent_version"]["value"] = "unapproved"
        with self.assertRaisesRegex(deploy.DeploymentError, "mismatched applied"):
            deploy.applied_targets(session)

    def test_typed_managed_projection_ignores_service_defaults(self):
        _, _, _, targets = fixture()
        for side, definition in targets["definitions"].items():
            kind = deploy.AGENT_SIDES[side]["kind"]
            reasoning = deploy.AGENT_SIDES[side]["reasoning"]
            actual = copy.deepcopy(definition)
            actual["id"] = "read-only"
            actual["created_at"] = 123
            actual["description"] = None
            if kind == "hosted":
                actual["container_configuration"]["registry_metadata"] = {"read_only": True}
                actual["protocol_versions"][0]["server_metadata"] = True
            else:
                actual["reasoning"]["summary"] = "auto"
            self.assertEqual(deploy.managed_definition(actual, kind, reasoning), definition)

    def test_hosted_environment_rejects_unexpected_extra_keys(self):
        _, _, _, targets = fixture()
        definition = copy.deepcopy(targets["definitions"]["hosted"])
        definition["environment_variables"]["UNEXPECTED_EXTRA"] = "value"
        with self.assertRaisesRegex(deploy.DeploymentError, "approved keys"):
            deploy.managed_definition(definition, "hosted", "low")

    def test_hosted_environment_rejects_missing_expected_keys(self):
        _, _, _, targets = fixture()
        definition = copy.deepcopy(targets["definitions"]["hosted"])
        del definition["environment_variables"]["SEARCH_INDEX_NAME"]
        with self.assertRaisesRegex(deploy.DeploymentError, "approved keys"):
            deploy.managed_definition(definition, "hosted", "low")

    def test_none_variant_drift_beyond_reasoning_is_refused(self):
        _, _, _, targets = fixture()
        definitions = copy.deepcopy(targets["definitions"])
        definitions["prompt_none"]["instructions"] = "Drifted instructions not present on prompt."
        with self.assertRaisesRegex(deploy.DeploymentError, "must be identical to prompt"):
            deploy.require_paired_reasoning_only_diff(definitions)

    def test_hosted_none_drift_beyond_reasoning_override_is_refused(self):
        _, _, _, targets = fixture()
        definitions = copy.deepcopy(targets["definitions"])
        definitions["hosted_none"]["cpu"] = "2"
        with self.assertRaisesRegex(deploy.DeploymentError, "must be identical to hosted"):
            deploy.require_paired_reasoning_only_diff(definitions)

    def test_hosted_none_image_mismatch_is_refused(self):
        _, _, _, targets = fixture()
        definitions = copy.deepcopy(targets["definitions"])
        definitions["hosted_none"]["container_configuration"]["image"] = (
            "exampleacr.azurecr.io/other@sha256:" + "b" * 64
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "must be identical to hosted"):
            deploy.require_paired_reasoning_only_diff(definitions)

    def test_properly_paired_definitions_are_accepted(self):
        _, _, _, targets = fixture()
        deploy.require_paired_reasoning_only_diff(copy.deepcopy(targets["definitions"]))

    def test_applied_targets_enforces_pairing(self):
        session, _, values, _ = fixture()
        resources = values["root_module"]["child_modules"][0]["resources"]
        for resource in resources:
            if resource["address"].endswith(".prompt_none"):
                resource["values"]["body"]["definition"]["instructions"] = "Drifted."
        with self.assertRaisesRegex(deploy.DeploymentError, "must be identical to prompt"):
            deploy.applied_targets(session)

    def test_exact_versions_compare_all_managed_fields(self):
        mutations = [
            ("hosted", ("container_configuration", "image"), "exampleacr.azurecr.io/other@sha256:" + "b" * 64),
            ("hosted", ("container_configuration", "image"), "exampleacr.azurecr.io/search-hosted:latest"),
            ("hosted", ("cpu",), "2"),
            ("hosted", ("cpu",), 1),
            ("hosted", ("memory",), "4Gi"),
            ("hosted", ("protocol_versions", 0, "version"), "1.0.0"),
            ("hosted", ("environment_variables", "MODEL_DEPLOYMENT_NAME"), "other-model"),
            ("hosted", ("environment_variables", "SEARCH_PROJECT_CONNECTION_ID"), "other-connection"),
            ("hosted", ("environment_variables", "SEARCH_INDEX_NAME"), "other-index"),
            ("hosted", ("environment_variables", "NEW_UNAPPROVED_SETTING"), "override"),
            ("prompt", ("instructions",), "Ignore the approved instructions."),
            ("prompt", ("model",), "other-model"),
            ("prompt", ("reasoning", "effort"), "high"),
            ("prompt", ("tools", 0, "azure_ai_search", "indexes", 0, "index_name"), "other-index"),
            ("prompt", ("tools", 0, "azure_ai_search", "indexes", 0, "project_connection_id"), "other"),
            ("prompt", ("tools", 0, "azure_ai_search", "indexes", 0, "query_type"), "semantic"),
            ("prompt", ("tools", 0, "azure_ai_search", "indexes", 0, "top_k"), True),
        ]
        for kind, path, value in mutations:
            session, _, _, targets = fixture()
            definitions = copy.deepcopy(targets["definitions"])
            node = definitions[kind]
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value

            def response(_session, _endpoint, requested_path, **kwargs):
                selected = "prompt" if "search-prompt" in requested_path else "hosted"
                return {"version": targets["agents"][selected]["version"], "status": "active",
                        "definition": definitions[selected]}

            with self.subTest(kind=kind, path=path), patch.object(deploy, "foundry_request", response):
                with self.assertRaises(deploy.DeploymentError):
                    deploy.active_versions(session, targets)

    def test_active_matching_exact_versions(self):
        session, _, _, targets = fixture()
        replies = [{"version": targets["agents"][kind]["version"], "status": "active",
                    "definition": definition} for kind, definition in targets["definitions"].items()]
        with patch.object(deploy, "foundry_request", side_effect=replies):
            self.assertEqual(deploy.active_versions(session, targets), [])

    def test_wrong_version_response_rejected(self):
        session, _, _, targets = fixture()
        with patch.object(deploy, "foundry_request", return_value={
            "version": "unapproved", "status": "active", "definition": targets["definitions"]["prompt"],
        }), self.assertRaisesRegex(deploy.DeploymentError, "different version"):
            deploy.active_versions(session, targets)


class RoutingTests(unittest.TestCase):
    def test_disabled_agents_rejected_even_when_routes_match(self):
        session, _, _, targets = fixture()
        for disabled in ("prompt", "hosted"):
            replies = [{"state": "disabled" if kind == disabled else "enabled",
                        "agent_endpoint": deploy.desired_route(agent["version"])}
                       for kind, agent in targets["agents"].items()]
            with self.subTest(disabled=disabled), patch.object(deploy, "foundry_request", side_effect=replies):
                with self.assertRaisesRegex(deploy.DeploymentError, "state=enabled"):
                    deploy.current_routes(session, targets)

    def test_verify_disabled_agent_never_patches_or_succeeds(self):
        session, _, _, _ = fixture()
        with (patch.object(deploy, "confirm_project"),
              patch.object(deploy, "active_versions", return_value=[]),
              patch.object(deploy, "foundry_request", return_value={"state": "disabled"}) as request,
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "state=enabled")):
            deploy.verify(session)
        self.assertTrue(all("patch" not in call.kwargs for call in request.call_args_list))

    def test_project_mismatch_stops_before_proposal_and_patch(self):
        session, _, _, _ = fixture()
        with (patch.object(deploy, "confirm_project", side_effect=deploy.DeploymentError("wrong project")),
              patch.object(deploy, "write_json") as write,
              patch.object(deploy, "foundry_request") as request,
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "wrong project")):
            deploy.route_agents(session)
        write.assert_not_called()
        request.assert_not_called()

    def test_definition_change_after_approval_stops_before_patch(self):
        session, _, _, _ = fixture()
        saved = {}

        def write(path, value):
            saved[path] = copy.deepcopy(value)

        with (patch.object(deploy, "confirm_project") as project,
              patch.object(deploy, "active_versions", side_effect=[[], deploy.DeploymentError("definition changed")]),
              patch.object(deploy, "current_routes", return_value=(
                  {side: {} for side in deploy.AGENT_SIDES}, {})),
              patch.object(deploy, "private_dir", side_effect=lambda path: path),
              patch.object(deploy, "write_json", side_effect=write),
              patch.object(deploy, "read_json", side_effect=lambda path: saved[path]),
              patch.object(deploy, "write_private"),
              patch.object(deploy, "confirm"),
              patch.object(deploy, "foundry_request") as request,
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "definition changed")):
            deploy.route_agents(session)
        self.assertEqual(project.call_count, 2)
        request.assert_not_called()

    def test_second_patch_rechecks_definitions_and_refuses_changed_version(self):
        session, _, _, targets = fixture()
        saved = {}
        routes = {side: {} for side in deploy.AGENT_SIDES}
        first_result = {"state": "enabled", "agent_endpoint": deploy.desired_route("1")}

        def write(path, value):
            saved[path] = copy.deepcopy(value)

        with (patch.object(deploy, "confirm_project") as project,
              patch.object(deploy, "active_versions",
                           side_effect=[[], [], [], deploy.DeploymentError("second version changed")]) as versions,
              patch.object(deploy, "current_routes", side_effect=[(routes, {}), (routes, {})]),
              patch.object(deploy, "private_dir", side_effect=lambda path: path),
              patch.object(deploy, "write_json", side_effect=write),
              patch.object(deploy, "read_json", side_effect=lambda path: saved[path]),
              patch.object(deploy, "write_private"),
              patch.object(deploy, "confirm"),
              patch.object(deploy, "foundry_request", side_effect=[None, first_result]) as request,
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "second version changed")):
            deploy.route_agents(session)
        patches = [call for call in request.call_args_list if "patch" in call.kwargs]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].args[2], deploy.agent_path(targets["agents"]["prompt"]))
        self.assertEqual(project.call_count, 3)
        self.assertEqual(versions.call_count, 4)

    def test_successful_verify_checks_applied_definition_and_never_mutates(self):
        session, _, _, targets = fixture()

        def response(_session, _endpoint, path, **kwargs):
            side = next(s for s in ("prompt_none", "hosted_none", "prompt", "hosted")
                        if targets["agents"][s]["name"] in path)
            agent = targets["agents"][side]
            if "/versions/" in path:
                return {"version": agent["version"], "status": "active",
                        "definition": targets["definitions"][side]}
            return {"state": "enabled", "agent_endpoint": deploy.desired_route(agent["version"])}

        with (patch.object(deploy, "confirm_project") as project,
              patch.object(deploy, "foundry_request", side_effect=response) as request,
              patch("sys.stdout", new_callable=io.StringIO)):
            deploy.verify(session)
        project.assert_called_once()
        self.assertEqual(request.call_count, 8)
        self.assertTrue(all("patch" not in call.kwargs for call in request.call_args_list))


class PermissionReadinessTests(unittest.TestCase):
    def run_readiness(self, search=None, agents=None, seconds=60):
        session, _, _, _ = fixture()
        with (patch.object(deploy, "confirm_project"),
              patch.object(deploy, "azure_request", side_effect=search or [{"value": []}, {}]) as search_request,
              patch.object(deploy, "foundry_request",
                           side_effect=agents or [{"data": []}] + [http_error(404)] * 4) as agent_request,
              patch.object(deploy.time, "sleep") as sleep,
              patch("sys.stdout", new_callable=io.StringIO)):
            deploy.permission_readiness(session, timeout=seconds)
        return search_request, agent_request, sleep

    def test_transient_forbidden_retries_then_success_and_absent_agents(self):
        search, agents, sleep = self.run_readiness(
            search=[http_error(403), {"value": []}, http_error(404)],
            agents=[http_error(403), {"data": []}, http_error(403), http_error(404), {},
                    http_error(404), http_error(404)])
        self.assertEqual(sleep.call_count, 3)
        self.assertIn("/indexes?$select=name&api-version=2024-07-01", search.call_args_list[0].args[1])
        self.assertEqual(search.call_args_list[0].args[2], "https://search.azure.com")
        self.assertIn("/indexes('documents')?api-version=2024-07-01", search.call_args_list[-1].args[1])
        self.assertEqual(agents.call_args_list[0].args[2], "/agents?api-version=v1")
        self.assertTrue(all("patch" not in call.kwargs for call in agents.call_args_list))

    def test_exhausted_forbidden_deadline_is_bounded(self):
        with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 1, 61]):
            with self.assertRaisesRegex(deploy.DeploymentError, "Timed out"):
                self.run_readiness(search=[http_error(403)])

    def test_collection_not_found_is_not_authorization(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.run_readiness(search=[http_error(404)])
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError):
            self.run_readiness(agents=[http_error(404)])

    def test_unauthorized_fails_with_reauthentication_guidance(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "Reauthenticate"):
            self.run_readiness(search=[http_error(401)])

    def test_other_errors_propagate_without_retry(self):
        for code in (400, 429, 500):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                self.run_readiness(search=[http_error(code)])

    def test_missing_foundation_refused_before_requests(self):
        session, _, _, _ = fixture()
        session.state.return_value = {}
        with patch.object(deploy, "azure_request") as request:
            with self.assertRaisesRegex(deploy.DeploymentError, "foundation"):
                deploy.permission_readiness(session)
        request.assert_not_called()

    def test_configured_direct_names_work_with_foundation_only_state(self):
        session, _, values, _ = fixture()
        del values["outputs"]["workload_agent_names"]
        values["outputs"]["deploy_workloads"]["value"] = False
        for side in deploy.AGENT_SIDES:
            values["outputs"][f"{side}_agent_version"]["value"] = None
        values["root_module"]["child_modules"][0]["resources"] = [
            resource for resource in values["root_module"]["child_modules"][0]["resources"]
            if not resource["address"].startswith("module.workloads[")
        ]
        with (patch.object(deploy, "confirm_project"),
              patch.object(deploy, "azure_request", side_effect=[{"value": []}, http_error(404)]),
              patch.object(deploy, "foundry_request",
                           side_effect=[{"data": []}] + [http_error(404)] * 4),
              patch("sys.stdout", new_callable=io.StringIO)):
            deploy.permission_readiness(session)
        session.tf.assert_not_called()

    def test_missing_parent_project_is_not_authorization(self):
        session, _, _, _ = fixture()
        with (patch.object(deploy, "confirm_project", side_effect=http_error(404)),
              patch.object(deploy, "foundry_request") as request,
              self.assertRaises(urllib.error.HTTPError)):
            deploy.permission_readiness(session)
        request.assert_not_called()

    def test_changed_foundation_refused(self):
        session, _, values, _ = fixture()
        session.state.side_effect = [values, {}]
        with (patch.object(deploy, "confirm_project"),
              patch.object(deploy, "azure_request", side_effect=[{"value": []}, {}]),
              patch.object(deploy, "foundry_request", side_effect=[{"data": []}, {}, {}, {}, {}]),
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "changed")):
            deploy.permission_readiness(session)

    def test_workload_plan_calls_gate_before_terraform_plan(self):
        session, outputs, _, _ = fixture()
        session.outputs.return_value = outputs
        with (patch.object(deploy, "image_inputs", return_value=({}, {})),
              patch.object(deploy, "permission_readiness",
                           side_effect=deploy.DeploymentError("permissions not ready")) as gate,
              self.assertRaisesRegex(deploy.DeploymentError, "permissions not ready")):
            deploy.plan_stage(session, "workloads")
        gate.assert_called_once()
        session.tf.assert_not_called()

    def test_workload_apply_gate_runs_after_approval_and_before_apply(self):
        session, outputs, _, _ = fixture()
        session.args.stage = "workloads"
        session.args.plan = "ignored-by-mock"
        session.outputs.return_value = outputs
        session.tfvars.read_bytes.return_value = b"tfvars"
        binary = deploy.ARTIFACTS / "plans" / "unit-test-unwritten" / "plan.bin"
        record = {"stage": "workloads", "binding": session.binding(),
                  "images": {}, "provenance": {}, "infra_inputs": {},
                  "sha256": deploy.digest(b"approved"),
                  "tfvars_sha256": deploy.digest(b"tfvars")}
        events = []

        def gate(*args, **kwargs):
            events.append("permission gate")
            raise deploy.DeploymentError("permissions not ready")

        with (patch.object(deploy, "input_path", return_value=binary),
              patch.object(deploy, "read_json", return_value=record),
              patch.object(deploy, "regular", return_value=Mock(read_bytes=lambda: b"approved")),
              patch.object(deploy, "infra_inputs", return_value={}),
              patch.object(deploy, "image_inputs", return_value=({}, {})),
              patch.object(deploy, "show_plan", return_value=({}, b"reviewed saved plan")),
              patch.object(deploy, "validate_plan"),
              patch.object(deploy, "confirm", side_effect=lambda _: events.append("approval")),
              patch.object(deploy, "permission_readiness", side_effect=gate),
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "permissions not ready")):
            deploy.apply_stage(session)
        self.assertEqual(events, ["approval", "permission gate"])
        session.tf.assert_not_called()


class WorkloadPlanTests(unittest.TestCase):
    def plan(self):
        session, _, _, _ = fixture()
        variables = {**session.target, "deploy_workloads": True, "hosted_image": "approved",
                     "web_image": "approved"}
        plan = {
            "variables": {key: {"value": value} for key, value in variables.items()},
            "planned_values": {"outputs": {"deploy_workloads": {"value": True}}},
            "resource_changes": [{
                "mode": "managed", "address": "module.search.azapi_data_plane_resource.index[0]",
                "change": {"actions": ["create"]},
            }],
        }
        return session, plan

    def test_workload_resources_and_unchanged_foundation_are_allowed(self):
        session, plan = self.plan()
        for address in ("module.workloads[0].azapi_data_plane_resource.prompt",
                        "module.workloads[0].azapi_data_plane_resource.hosted",
                        "module.workloads[0].azapi_data_plane_resource.prompt_none",
                        "module.workloads[0].azapi_data_plane_resource.hosted_none",
                        "module.workloads[0].azapi_resource.web"):
            plan["resource_changes"].append({"mode": "managed", "address": address,
                                             "change": {"actions": ["create"]}})
        plan["resource_changes"] += [
            {"mode": "managed", "address": "module.foundry.azapi_resource.project",
             "change": {"actions": ["no-op"]}},
            {"mode": "data", "address": "data.azapi_client_config.current",
             "change": {"actions": ["read"]}},
        ]
        deploy.validate_plan(session, plan, "workloads",
                             {"hosted_image": "approved", "web_image": "approved"})

    def test_foundation_mutations_fail_saved_plan_validation(self):
        for address in (
            "module.foundry.azapi_resource.account",
            "module.foundry.azapi_resource.project",
            "module.foundry.azapi_resource.model",
            "module.search.azapi_resource.service",
            "module.search.azapi_resource.execution_schema_access",
            "module.access.azapi_resource.role_assignment",
        ):
            for actions in (["create"], ["update"], ["delete"], ["delete", "create"], ["create", "delete"]):
                session, plan = self.plan()
                plan["resource_changes"].append({"mode": "managed", "address": address,
                                                 "change": {"actions": actions}})
                with self.subTest(address=address, actions=actions):
                    with self.assertRaisesRegex(deploy.DeploymentError, "foundational resources"):
                        deploy.validate_plan(session, plan, "workloads",
                                             {"hosted_image": "approved", "web_image": "approved"})

    def test_known_planned_definitions_validate_without_applied_agent_state(self):
        session, plan = self.plan()
        _, _, values, _ = fixture()
        plan["planned_values"]["root_module"] = values["root_module"]
        session.state.side_effect = AssertionError("No applied agent state exists before first apply")
        deploy.validate_plan(session, plan, "workloads",
                             {"hosted_image": "approved", "web_image": "approved"})

    def test_bad_known_planned_definition_is_refused_before_apply(self):
        for field, value in (("reasoning", {"effort": "high"}), ("max_output_tokens", 500)):
            session, plan = self.plan()
            _, _, values, _ = fixture()
            resources = values["root_module"]["child_modules"][0]["resources"]
            resources[-2]["values"]["body"]["definition"][field] = value
            plan["planned_values"]["root_module"] = values["root_module"]
            with self.subTest(field=field), self.assertRaises(deploy.DeploymentError):
                deploy.validate_plan(session, plan, "workloads",
                                     {"hosted_image": "approved", "web_image": "approved"})


class BuildxPluginTests(unittest.TestCase):
    def test_uses_installed_plugin_directory_without_global_configuration(self):
        path = Path(__file__).resolve().parent / "docker-buildx"
        with patch.object(Path, "is_file", return_value=True), patch.object(deploy.os, "access", return_value=True):
            self.assertEqual(
                deploy.buildx_plugin_directory([{"Name": "buildx", "Path": str(path)}]),
                str(path.parent),
            )

    def test_rejects_missing_invalid_or_failed_plugin_metadata(self):
        for plugins in (None, [None], [], [{"Name": "compose"}],
                        [{"Name": "buildx", "Err": "unavailable"}],
                        [{"Name": "buildx", "Path": "docker-buildx"}],
                        [{"Name": "buildx", "Path": None}]):
            with self.subTest(plugins=plugins), self.assertRaises(deploy.DeploymentError):
                deploy.buildx_plugin_directory(plugins)

    def test_rejects_nonexecutable_plugin(self):
        path = Path(__file__).resolve().parent / "docker-buildx"
        with patch.object(Path, "is_file", return_value=True), patch.object(deploy.os, "access", return_value=False):
            with self.assertRaises(deploy.DeploymentError):
                deploy.buildx_plugin_directory([{"Name": "buildx", "Path": str(path)}])


class AzureRequestTests(unittest.TestCase):
    def test_audience_tokens_and_read_only_requests(self):
        session, _, _, _ = fixture()
        response = Mock(status=200)
        response.read.return_value = b'{"data":[]}'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        with (patch.object(deploy, "command", return_value=Mock(stdout=b"test-token")) as command,
              patch.object(deploy, "executable", return_value=["az"]),
              patch.object(deploy.urllib.request, "build_opener", return_value=opener)):
            deploy.foundry_request(session, ENDPOINT, "/agents?api-version=v1")
            deploy.azure_request(session, "https://example-search.search.windows.net/indexes?api-version=2024-07-01",
                                 "https://search.azure.com")
        self.assertEqual(command.call_count, 2)
        for call, audience in zip(command.call_args_list, ("https://ai.azure.com", "https://search.azure.com")):
            arguments = call.args[0]
            self.assertEqual(arguments[arguments.index("--resource") + 1], audience)
            self.assertEqual(arguments[arguments.index("--subscription") + 1], SUBSCRIPTION)
        for call in opener.open.call_args_list:
            self.assertEqual(call.args[0].method, "GET")
            self.assertIsNone(call.args[0].data)

    def test_redirect_and_audience_mismatch_refused(self):
        session, _, _, _ = fixture()
        with patch.object(deploy, "command") as command:
            with self.assertRaises(deploy.DeploymentError):
                deploy.azure_request(session, "https://example.invalid", "https://ai.azure.com")
        command.assert_not_called()
        self.assertIsNone(deploy.NoRedirect().redirect_request(None, None, 302, None, None,
                                                              "https://example.invalid"))


class ProjectSearchRbacTests(unittest.TestCase):
    def context_fixture(self):
        session, outputs, values, _ = fixture()
        outputs["project_principal_id"] = SUBSCRIPTION
        values["outputs"]["project_principal_id"] = {"value": SUBSCRIPTION}
        resources = values["root_module"]["child_modules"][0]["resources"]
        resources[0]["values"]["identity"] = [{"principal_id": SUBSCRIPTION}]
        resources.append({
            "address": "module.workloads[0].azapi_resource.web", "mode": "managed",
            "values": {"body": {"properties": {"template": {"containers": [{
                "name": "web", "image": "exampleacr.azurecr.io/search-web@sha256:" + "b" * 64,
            }]}}}},
        })
        session.registry.return_value = ("exampleacr", "exampleacr.azurecr.io")
        with (patch.object(deploy, "confirm_project", return_value={"identity": {"principalId": SUBSCRIPTION}}),
              patch.object(deploy, "azure_request", return_value={"id": SEARCH})):
            context = deploy.project_search_rbac_context(session)
        return session, outputs, values, context

    def plan_fixture(self):
        session, outputs, values, context = self.context_fixture()
        changes = []
        for key, role in deploy.PROJECT_SEARCH_ROLES.items():
            name = str(deploy.uuid.uuid5(deploy.uuid.NAMESPACE_URL, f"{SEARCH}/{SUBSCRIPTION}/{role}".lower()))
            changes.append({
                "address": f'module.access.azapi_resource.assignment["{key}"]',
                "mode": "managed", "type": "azapi_resource",
                "change": {"actions": ["create"], "before": None, "after": {
                    "type": "Microsoft.Authorization/roleAssignments@2022-04-01",
                    "parent_id": SEARCH, "name": name,
                    "body": {"properties": {
                        "principalId": SUBSCRIPTION, "principalType": "ServicePrincipal",
                        "roleDefinitionId": (f"/subscriptions/{SUBSCRIPTION}"
                                             f"/providers/Microsoft.Authorization/roleDefinitions/{role}"),
                    }},
                }, "after_unknown": {"id": True, "body": {"properties": {}}}},
            })
        variables = {**session.target, "deploy_workloads": True, **context["images"]}
        plan = {
            "variables": {key: {"value": value} for key, value in variables.items()},
            "prior_state": {"values": values}, "planned_values": values,
            "resource_changes": changes,
            "output_changes": {"deploy_workloads": {"actions": ["no-op"]}},
            "configuration": {"root_module": {"module_calls": {"access": {"module": {"resources": [{
                "address": "azapi_resource.assignment",
                "expressions": {key: {} for key in ("body", "name", "parent_id", "type")},
            }]}}}}},
        }
        return session, outputs, values, context, plan

    def validate(self, session, context, plan):
        deploy.validate_plan(session, plan, "project-search-rbac", context["images"], rbac_context=context)

    def test_exact_two_creates_preserve_existing_workloads(self):
        session, _, _, context, plan = self.plan_fixture()
        self.validate(session, context, plan)

    def test_context_pins_applied_images_without_current_provenance(self):
        session, _, _, context = self.context_fixture()
        with (patch.object(deploy, "confirm_project", return_value={"identity": {"principalId": SUBSCRIPTION}}),
              patch.object(deploy, "azure_request", return_value={"id": SEARCH}) as request,
              patch.object(deploy, "image_inputs", side_effect=AssertionError("unapplied images are not a source"))):
            self.assertEqual(deploy.project_search_rbac_context(session), context)
        self.assertEqual(request.call_args.args[1], "https://management.azure.com" + SEARCH + "?api-version=2025-05-01")
        self.assertEqual(request.call_args.args[2], "https://management.azure.com/")

    def test_wrong_arm_principal_or_scope_refused(self):
        for principal, scope in (("different", SEARCH), (SUBSCRIPTION, SEARCH + "-other")):
            session, _, _, _ = self.context_fixture()
            with (self.subTest(principal=principal, scope=scope),
                  patch.object(deploy, "confirm_project", return_value={"identity": {"principalId": principal}}),
                  patch.object(deploy, "azure_request", return_value={"id": scope}),
                  self.assertRaises(deploy.DeploymentError)):
                deploy.project_search_rbac_context(session)

    def test_foundation_only_state_cannot_use_rbac_repair(self):
        session, _, values, _ = self.context_fixture()
        values["outputs"]["deploy_workloads"]["value"] = False
        with patch.object(deploy, "azure_request") as request, self.assertRaises(deploy.DeploymentError):
            deploy.project_search_rbac_context(session)
        request.assert_not_called()

    def test_other_resources_and_noncreate_actions_refused(self):
        for action in (["update"], ["delete"], ["create", "delete"], ["delete", "create"]):
            session, _, _, context, plan = self.plan_fixture()
            plan["resource_changes"][0]["change"]["actions"] = action
            with self.subTest(action=action), self.assertRaises(deploy.DeploymentError):
                self.validate(session, context, plan)
        session, _, _, context, plan = self.plan_fixture()
        plan["resource_changes"].append({"mode": "managed", "type": "azapi_resource",
                                        "address": "module.workloads[0].azapi_resource.web",
                                        "change": {"actions": ["update"]}})
        with self.assertRaises(deploy.DeploymentError):
            self.validate(session, context, plan)

    def test_exact_principal_roles_scope_and_properties_required(self):
        for path, value in (
            (("parent_id",), SEARCH + "-other"),
            (("name",), "00000000-0000-0000-0000-000000000000"),
            (("body", "properties", "principalId"), "other-principal"),
            (("body", "properties", "roleDefinitionId"), "other-role"),
            (("body", "properties", "principalType"), "User"),
            (("body", "properties", "condition"), "unapproved"),
        ):
            session, _, _, context, plan = self.plan_fixture()
            node = plan["resource_changes"][0]["change"]["after"]
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(deploy.DeploymentError):
                self.validate(session, context, plan)

    def test_missing_duplicate_or_extra_assignment_refused(self):
        for alteration in ("missing", "duplicate", "extra"):
            session, _, _, context, plan = self.plan_fixture()
            if alteration == "missing":
                plan["resource_changes"].pop()
            else:
                extra = copy.deepcopy(plan["resource_changes"][0])
                if alteration == "extra":
                    extra["address"] = 'module.access.azapi_resource.assignment["ui_search_data"]'
                plan["resource_changes"].append(extra)
            with self.subTest(alteration=alteration), self.assertRaises(deploy.DeploymentError):
                self.validate(session, context, plan)

    def test_unknown_hidden_import_or_moved_requests_refused(self):
        for alteration in ("unknown", "hidden", "import", "move", "query"):
            session, _, _, context, plan = self.plan_fixture()
            resource = plan["resource_changes"][0]
            if alteration == "unknown":
                resource["change"]["after_unknown"]["body"] = {"properties": {"principalId": True}}
            elif alteration == "hidden":
                resource["change"]["after"]["sensitive_body_version"] = {"body": "secret"}
            elif alteration == "import":
                resource["change"]["importing"] = {"id": "unapproved"}
            elif alteration == "move":
                resource["previous_address"] = "module.other.assignment"
            else:
                resource["change"]["after"]["create_query_parameters"] = {"unapproved": ["true"]}
            with self.subTest(alteration=alteration), self.assertRaises(deploy.DeploymentError):
                self.validate(session, context, plan)

    def test_write_only_config_and_provisioners_cannot_hide_from_plan_values(self):
        for field in ("sensitive_body", "ignore_body_changes", "provisioners"):
            session, _, _, context, plan = self.plan_fixture()
            resource = plan["configuration"]["root_module"]["module_calls"]["access"]["module"]["resources"][0]
            if field == "provisioners":
                resource[field] = [{"type": "local-exec"}]
            else:
                resource["expressions"][field] = {"references": ["var.unapproved"]}
            with self.subTest(field=field), self.assertRaisesRegex(deploy.DeploymentError, "reviewed assignment schema"):
                self.validate(session, context, plan)

    def test_state_stage_images_and_output_changes_are_bound(self):
        for alteration in ("stage", "images", "principal", "output"):
            session, _, _, context, plan = self.plan_fixture()
            if alteration == "stage":
                plan["variables"]["deploy_workloads"]["value"] = False
            elif alteration == "images":
                plan["variables"]["hosted_image"]["value"] = "new-unapplied-image"
            elif alteration == "principal":
                plan["prior_state"]["values"]["outputs"]["project_principal_id"]["value"] = "other"
            else:
                plan["output_changes"]["hosted_agent_version"] = {"actions": ["update"]}
            with self.subTest(alteration=alteration), self.assertRaises(deploy.DeploymentError):
                self.validate(session, context, plan)

    def test_planning_uses_applied_image_overrides_and_never_applies(self):
        session, outputs, _, context, plan = self.plan_fixture()
        session.outputs.return_value = outputs
        session.tfvars = Mock(read_bytes=lambda: b"private tfvars")
        saved = {}
        with (patch.object(deploy, "project_search_rbac_context", return_value=context),
              patch.object(deploy, "image_inputs", side_effect=AssertionError("no build provenance")),
              patch.object(deploy, "permission_readiness", side_effect=AssertionError("no data-plane grant needed")),
              patch.object(deploy, "infra_inputs", return_value={}),
              patch.object(deploy, "private_dir", side_effect=lambda path: path),
              patch.object(deploy, "write_json", side_effect=lambda path, value: saved.update({path: value})),
              patch.object(deploy, "write_private"),
              patch.object(deploy, "regular", return_value=Mock()),
              patch.object(Path, "read_bytes", return_value=b"binary"),
              patch.object(deploy, "show_plan", return_value=(plan, b"Two approved role creates")),
              patch.object(deploy, "confirm", side_effect=AssertionError("plan must never request apply")),
              patch("sys.stdout", new_callable=io.StringIO)):
            deploy.plan_stage(session, "project-search-rbac")
        self.assertEqual(session.tf.call_args.args[0], "plan")
        self.assertNotIn(f"-var-file={deploy.ARTIFACTS / 'images.tfvars.json'}", session.tf.call_args.args)
        overrides = next(value for path, value in saved.items() if path.name == "stage.tfvars.json")
        self.assertIs(overrides["deploy_workloads"], True)
        self.assertEqual(overrides["hosted_image"], context["images"]["hosted_image"])
        approval = next(value for path, value in saved.items() if path.name == "approval.json")
        self.assertEqual(approval["rbac_context"], context)
        self.assertEqual(approval["sha256"], deploy.digest(b"binary"))

    def test_eventual_apply_still_requires_approval_and_fresh_arm_binding(self):
        session, outputs, _, context, plan = self.plan_fixture()
        session.outputs.return_value = outputs
        session.args.stage = "project-search-rbac"
        session.args.plan = "mock-path"
        session.tfvars = Mock(read_bytes=lambda: b"private tfvars")
        record = {
            "stage": "project-search-rbac", "binding": session.binding(),
            "images": context["images"], "rbac_context": context,
            "infra_inputs": {}, "tfvars_sha256": deploy.digest(b"private tfvars"),
            "sha256": deploy.digest(b"approved binary"),
        }
        changed = {**context, "principal_id": "changed-after-approval"}
        with (patch.object(deploy, "project_search_rbac_context", side_effect=[context, changed]),
              patch.object(deploy, "input_path", return_value=deploy.ARTIFACTS / "plans" / "unwritten" / "plan.bin"),
              patch.object(deploy, "read_json", return_value=record),
              patch.object(deploy, "regular", return_value=Mock(read_bytes=lambda: b"approved binary")),
              patch.object(deploy, "infra_inputs", return_value={}),
              patch.object(deploy, "image_inputs", side_effect=AssertionError("no unapplied image provenance")),
              patch.object(deploy, "show_plan", return_value=(plan, b"two creates")),
              patch.object(deploy, "confirm") as approval,
              patch("sys.stdout", new_callable=io.StringIO),
              self.assertRaisesRegex(deploy.DeploymentError, "scope/principal changed after approval")):
            deploy.apply_stage(session)
        approval.assert_called_once_with("APPROVE " + record["sha256"])
        session.tf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
