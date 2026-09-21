# ACA Comparison Agents

The third comparison row adds `aca` (low reasoning) and `aca_none` (no reasoning).
Both run the existing hosted image as separate internal Azure Container Apps.
The original four Foundry registrations and the UI's user-assigned identity are
unchanged. Each new app uses its own system-assigned identity and dedicated model
deployment, with the same model version and capacity as the other four.

Both apps reuse `config/agent.json` and Foundry's native Azure AI Search tool with
the existing project connection and index. Their identities authenticate to
Foundry, not directly to Search. They receive only AcrPull on the shared registry
and Foundry User on the existing project. Existing Foundry Search grants remain.

## Deployment

Use `deploy.py` with the existing explicit environment, private tfvars, target
and backend requirements. Do not use raw Terraform apply or disable workloads
in an existing deployment. No cloud resources are created by local tests.

The current example allocates 64 to each of the six models. At the last quota
check, other deployments consumed 610, so the expected regional total is 994
of 1,000. Recheck usage before creating models; this leaves only six units free.
For the existing four deployments at 80, first run `plan-model-capacity` with
the normal environment/tfvars arguments, then separately approve its exact saved
plan using `apply --stage model-capacity`. This stage only reduces the four
existing dedicated capacities equally. It preserves deployed images, model
versions, foundation resources and the legacy deployment, and needs no rebuild.
Wait for quota usage to reflect the reduction before the ACA bootstrap below.

1. Add `aca` and `aca_none` entries to the private `model_deployments` map. All
   six names must be distinct and capacities equal. Verify regional quota for
   all six deployments plus the retained legacy foundation deployment.
2. Complete foundation setup only for a new environment. Existing environments
   keep their foundation and UI identity. Build/push the two images through the
   separately approved `build-images` command; there is no third image.
3. Run `plan-aca-bootstrap --environment ENV --tfvars PATH`. This deliberately
   targets the four new role assignments and their dependencies. Review the
   saved plan: it permits only new ACA apps, their four grants, and new dedicated
   model deployments. Existing resources must remain unchanged.
4. Apply that exact saved plan with `apply --stage aca-bootstrap --plan PATH
   --environment ENV --tfvars PATH` and its separate `APPROVE <sha256>` prompt.
5. Run `plan-workloads --environment ENV --tfvars PATH`, review, and separately
   apply with `--stage workloads`. This installs the approved private hosted
   image and adds the two internal URLs to the web backend. Bootstrap grants
   must already exist; app replacements and grant changes are refused.
6. Use the existing separately approved `route-agents` step for the four Foundry
   agents only. Run `verify` to check those routes, ACA latest ready revisions,
   system principals, images, runtime configuration, and UI health.

Every plan remains bound to its saved binary hash, inputs, target, and approved
image provenance. The bootstrap stage does not update the existing UI. Its
public Microsoft quickstart image is pinned by digest, receives no runtime env
or secrets, and has managed-identity access disabled (`lifecycle=None`). Internal
ingress allows its FQDN to be allocated. The subsequent private revision enables
identity access only for the main container and runs one replica per agent.
Never re-bootstrap an active app. A partially applied bootstrap may be retried
only when the guard still permits a create-only plan; replacement or identity
repair needs separate review. Allow Azure time to propagate AcrPull/Foundry RBAC
before deploying or testing the private runtime; readiness probes do not prove
inference authorization.

## Live Acceptance

Local tests and ARM readiness are not proof that a new system identity can run
the project's model or native Search. After deployment, submit a known indexed
book question through the UI and check all six answers, native Search evidence,
response IDs, and traces. Test a follow-up and reset. ACA history must be replayed
without a Foundry stored conversation, continuation, or sandbox session ID.
Confirm one failing agent does not discard the other five histories.

Check that both ACA endpoints are inaccessible from outside their Container Apps
environment while the web backend can reach them. Internal ingress is an
environment-level trust boundary, not per-caller Entra authentication. Do not
expose these runtimes publicly without a separately reviewed authentication design.

## Local Checks

```sh
uv run pytest tests deployment/test_deploy.py deployment/test_import_books.py
node --test src/web/static/app.test.cjs
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
terraform -chdir=infra/modules/workloads init -backend=false -input=false
terraform -chdir=infra/modules/workloads test
```

The workload Terraform tests use a mocked provider and do not contact Azure.