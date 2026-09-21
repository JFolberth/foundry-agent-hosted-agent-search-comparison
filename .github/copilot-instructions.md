# Copilot instructions — foundry-agent-hosted-agent-search-comparison

## Objective

This project compares the **performance of multiple agent hosting providers**, all
grounded in the **same Azure AI Search index** and the same instructions
([config/agent.json](../config/agent.json)). It is a benchmarking/demo tool, not a
production agent product. Any change should preserve fair, apples-to-apples
comparison across sides: identical model name/version/capacity, identical search
index and query settings, identical instructions — only the hosting mechanism and
reasoning effort differ.

## Architecture snapshot

Six comparison "sides", each a distinct hosting approach paired with a reasoning level:

| Side          | Hosting                                   | Reasoning |
|---------------|--------------------------------------------|-----------|
| `prompt_none` | Foundry-managed prompt agent               | none      |
| `prompt`      | Foundry-managed prompt agent               | low       |
| `hosted_none` | Foundry hosted-agent container             | none      |
| `hosted`      | Foundry hosted-agent container             | low       |
| `aca_none`    | Standalone Azure Container Apps runtime    | none      |
| `aca`         | Standalone Azure Container Apps runtime    | low       |

- `src/comparison/` — shared service logic (`service.py` fans out all sides in
  parallel per request), agent registry (`agents.py`), config, telemetry, diagnostics.
- `src/hosted_agent/` — container app used by the `hosted`/`hosted_none` sides.
- `src/web/` — FastAPI + static JS/CSS UI (`src/web/static/app.js`) that renders the
  side-by-side comparison and a client-side (localStorage-backed) "last 14 runs"
  latency chart, rendered immediately on page load.
- `infra/` — Terraform (azapi provider only). One shared platform (Log Analytics,
  Application Insights, ACR, Container Apps environment, Foundry account/project,
  Search service) plus per-side model deployments and agent/ACA registrations.
- `deployment/deploy.py` — the **only** sanctioned way to plan/apply infra or build
  images. It is stdlib-only Python with strict staged approvals; see below.

## Deployment process — read this before touching infra or images

Do **not** run raw `terraform apply`, `terraform plan`, or `docker push` by hand.
Always use `deployment/deploy.py`. It enforces its own safety rails and will refuse
in ways that look like bugs but are intentional — do not "route around" a refusal.

### Prerequisites (already true in this dev container)
- Azure CLI signed in to the target subscription/tenant (`az login`).
- Docker with buildx.
- Terraform, using the existing provider lock file.

### Session target comes from the CLI login, not the tfvars file
`deploy.py` reads `DEPLOY_SUBSCRIPTION_ID` and `DEPLOY_TENANT_ID` from the
environment, cross-checks them against `az account show`, and **injects them as a
final `-var-file` override that wins over anything in `--tfvars`.** This is why
`infra/environments/example.tfvars` is committed with placeholder
`00000000-0000-0000-0000-000000000000` subscription/tenant IDs — they are never
actually used for a real apply. Do not hand-edit real subscription/tenant IDs into
that file. Just export:
```
export DEPLOY_SUBSCRIPTION_ID=<subscription-guid>
export DEPLOY_TENANT_ID=<tenant-guid>
```
matching whatever `az account show` currently reports.

### The `--tfvars` path must match the original `init` exactly
Each environment's first `deploy.py init` records a binding (target + tfvars path +
backend hash) in `deployment/.artifacts/<env>/initialized.json`. Every later stage
compares against that exact recorded path string. For the live `dev` environment in
this repo, the bound file is `infra/environments/example.tfvars` — use that path
verbatim. Passing a different (even content-identical) tfvars path fails with:
`Refused: Backend/target/environment file changed; use the original initialized environment.`
Don't try to work around this by re-running `init`; find/use the file the
environment was actually bound to.

### Approval prompts require a real interactive terminal
`build-images` and `apply` print a hash and require you to type it back exactly
(`BUILD AND PUSH <sha256>` / `APPROVE <sha256>`). Piping output (e.g. `| tail`)
triggers `Refused: Approval requires an interactive terminal; piping approval is
forbidden.` Run these commands directly in a terminal (or drive a real terminal
session), read the printed plan/hash, then send the exact approval string.

### Staged flow (append `--environment ENV --tfvars PATH` to every command)
1. `plan-foundation` → `apply --stage foundation --plan <path>` — platform only.
2. `build-images` — builds+pushes `hosted` and `web` images to the shared ACR with
   real digests; writes `deployment/.artifacts/images.tfvars.json` privately.
3. `plan-workloads` — **always read the diff.** For a pure app/UI code change this
   should show `0 to add, 1 to change, 0 to destroy` (just the container image
   digest + `DEPLOYED_AT`). If it shows anything touching agents, models, Search,
   or RBAC, stop and investigate before applying — that means something drifted
   or the wrong tfvars/environment was targeted.
4. `apply --stage workloads --plan <path>` — approve only after confirming the
   plan is minimal and expected. This step can take a couple of minutes (permission
   readiness checks + Container Apps revision rollout with health probes) — don't
   assume it's hung.
5. `route-agents` — only needed when Foundry agent versions changed, not for a
   pure web-UI image update.
6. `verify` — read-only; confirms agent versions active and UI `/health` is 200.

### Minimal churn for a UI-only change (the common case)
For changes confined to `src/web/static/` (or other web-container-only code), you
only need steps 2–4 (`build-images` → `plan-workloads` → `apply --stage workloads`)
plus `verify`. Skip foundation/model-capacity/aca-bootstrap/route-agents entirely —
touching those stages for an unrelated UI change is unnecessary churn and risks
unrelated diffs.

### Never do these
- Never hand-edit `deploy_workloads` in tfvars to "restage" a live environment —
  it deletes workloads.
- Never target a different tfvars file than what an environment was `init`-ed with.
- Never commit anything under `deployment/.artifacts/` or `infra/environments/*.tfvars`
  (except `example.tfvars`) — both are gitignored for a reason (build provenance /
  real subscription-scoped values).

## Testing
- Python: `pytest` from repo root (uses `pyproject.toml`); `deployment/test_deploy.py`
  and `deployment/test_import_books.py` are stdlib-only, run separately (not part of
  the default `pyproject.toml` test path), and run without cloud access.
- Web static JS: `node --test src/web/static/app.test.cjs` (no build step, no
  external test runner — plain Node's built-in `node:test`).
- Run both suites before considering a change done. A failing test is never left
  around: either it caught a real regression (fix the code), or it encodes a
  now-outdated assumption (update or remove the test in the same change). Don't
  leave a broken test "for later" or skip/xfail it as a workaround.
- `deployment/test_deploy.py`'s `BackendTests` read the actual checked-in
  `infra/backend.tf` (currently a real, migrated `azurerm` remote backend, not
  `local`) and can be tripped by stray local Terraform artifacts. If you run raw
  `terraform init`/`plan` in `infra/` for read-only exploration, clean up the
  `infra/.terraform/` directory it creates afterward (it's gitignored and
  unrelated to `deploy.py`'s own isolated state under `deployment/.artifacts/`),
  or those tests will fail for reasons that have nothing to do with your change.

## Conventions worth preserving
- `src/web/static/*` ships under a strict CSP (`style-src 'self'`, no inline
  `style=`/`.style.` usage, no `.innerHTML`). Prefer CSS classes/custom properties
  or SVG presentation attributes (e.g. `rect.setAttribute('width', ...)`) over
  anything that needs inline styles.
- `src/comparison/evidence.py` and `diagnostics.py` sanitize/allow-list everything
  that reaches the client or telemetry (URLs, error types, tool-call payloads).
  Don't bypass these when surfacing new fields — treat model/tool output as
  untrusted.
- Telemetry (`src/comparison/telemetry.py`) uses one shared, workspace-based
  Application Insights resource for all sides; spans are tagged with
  `comparison.id` / `comparison.side` for cross-agent correlation in Log Analytics
  (`AppDependencies`, not `AppRequests` — spans are `SpanKind.INTERNAL` by default).
