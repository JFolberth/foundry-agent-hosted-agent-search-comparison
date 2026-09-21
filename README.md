# Foundry Agent Hosting and Search Comparison

A benchmarking and demo application that compares six agent configurations using
the same model, shared instructions, and Azure AI Search index. Submit one book
catalog question to run the configured sides in parallel and compare their
answers, latency, token usage, citations, and exposed native tool-call evidence.

This is not a production agent product. The web UI is publicly accessible without
user authentication in the deployed configuration; review access controls and
cost exposure before using it outside a controlled demo.

## Comparison Sides

| Side | Hosting | Reasoning | Runtime compute |
| --- | --- | --- | --- |
| `prompt_none` | Foundry-managed prompt agent | `none` | Managed by Foundry |
| `prompt` | Foundry-managed prompt agent | `low` | Managed by Foundry |
| `hosted_none` | Foundry hosted-agent container | `none` | 1 vCPU, 2 GiB |
| `hosted` | Foundry hosted-agent container | `low` | 1 vCPU, 2 GiB |
| `aca_none` | Standalone Azure Container Apps runtime | `none` | 1 vCPU, 2 GiB |
| `aca` | Standalone Azure Container Apps runtime | `low` | 1 vCPU, 2 GiB |

Each ACA agent uses the Consumption workload profile with one replica
(`minReplicas=1`, `maxReplicas=1`) and internal ingress. Foundry manages hosted-agent
instances. The separate web container uses 0.5 vCPU and 1 GiB.

The [example environment](infra/environments/example.tfvars) configures
`gpt-5.6-terra`, version `2026-07-09`, with six distinct model deployments at equal
capacity (64 each). Model availability and regional quota must be checked for
your subscription. Capacity is a model deployment setting, not container CPU.

Keep model name, version, capacity, instructions, index, and Search settings equal
across sides. Only hosting and reasoning effort should vary in the comparison.

## Architecture

- The FastAPI web backend fans each request out to the configured agents in parallel.
- Prompt agents use Foundry-managed definitions with native Azure AI Search.
- Hosted and ACA agents run the same container image and call the project model
  Responses API with the same native Search tool.
- All sides share one Search index and the instructions in
  [config/agent.json](config/agent.json).
- Application Insights and Log Analytics correlate spans using `comparison.id`
  and `comparison.side`. The UI shows recent latency history, including the last
  14 runs, with server-backed history and browser-local persistence.

Managed prompt agents use stored conversation state; hosted and ACA runtimes
replay their own per-side histories. Hosted session IDs support sandbox routing,
not conversation storage. Start a fresh comparison when measuring first-turn
behavior: differing earlier answers also create differing follow-up context.

| Path | Purpose |
| --- | --- |
| [src/comparison](src/comparison) | Shared configuration, clients, fan-out, evidence, telemetry, and history |
| [src/hosted_agent](src/hosted_agent) | Runtime shared by Foundry hosted agents and ACA |
| [src/web](src/web) | FastAPI backend and static JavaScript/CSS UI |
| [config/agent.json](config/agent.json) | Shared instructions and fixed Search/model-call settings |
| [infra](infra) | Terraform infrastructure using the AzAPI provider |
| [deployment](deployment) | Staged deployment, data ingestion, and deployment tests |
| [data/source.json](data/source.json) | Dataset provenance, checksum, mapping, and limitations |
| [tests](tests) | Application tests |

## Local Development

Use Python **3.11**, [uv](https://docs.astral.sh/uv/), and Node.js with built-in
`node:test` support. Cloud-backed execution also needs Azure CLI authentication
and permissions on the deployed Foundry project. Deployment additionally requires
Terraform with the checked-in provider lock file and Docker with buildx.

Install the locked application and development dependencies from the repository root:

```sh
uv sync --frozen
```

### Run the Web App

Local startup connects to existing Azure resources; it does not provision agents
or create an index. Replace the placeholders below with values from your deployed
environment, then run:

```sh
az login
export COMPARISON_LOCAL_DEVELOPMENT=true
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export MODEL_DEPLOYMENT_NAME="<model-deployment>"
export SEARCH_PROJECT_CONNECTION_ID="/subscriptions/<subscription>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>/connections/<connection>"
export SEARCH_INDEX_NAME="public-documents"
export PROMPT_AGENT_NAME="search-prompt"
export HOSTED_AGENT_NAME="search-hosted"
export PROMPT_AGENT_NAME_NONE="search-prompt-none"
export HOSTED_AGENT_NAME_NONE="search-hosted-none"
uv run uvicorn web.app:app --app-dir src --host 127.0.0.1 --port 8080
```

Open <http://localhost:8080>. Local mode uses `DefaultAzureCredential`; deployed
containers use managed identities. The two `*_NAME_NONE` variables are optional
for environments that only have the low-reasoning pair.

For all six sides, also configure `ACA_ENDPOINT` and `ACA_ENDPOINT_NONE` with
distinct internal HTTPS Container Apps URLs. Both must be supplied together, and
the web backend must have network access to them. A normal local workstation
cannot reach these internal endpoints; use the deployed web app for the complete
six-way comparison. Do not make the ACA runtimes public as a workaround.

Optional telemetry settings:

| Variable | Purpose |
| --- | --- |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Application telemetry export |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace customer GUID, not its ARM resource ID, for latency-history queries |

The querying identity needs Log Analytics read access. When history queries are
unavailable, the API reports history as unavailable rather than failing the
comparison service.

### Tests

Run both suites from the repository root. These tests do not require cloud access.
Deployment tests are listed explicitly because the default pytest path only
includes application tests.

```sh
uv run pytest tests deployment/test_deploy.py deployment/test_import_books.py deployment/test_model_capacity.py
node --test src/web/static/app.test.cjs
```

The static UI has no frontend build step. Preserve its strict content security
policy: no inline styles or `innerHTML` rendering of model/tool output.

## Deployment

**Use [deployment/deploy.py](deployment/deploy.py) for every infrastructure plan,
apply, and image build/push.** Do not run raw `terraform plan`, `terraform apply`,
or `docker push`. Each stage checks target bindings, inputs, and approval hashes.

Select the intended Azure CLI account and set the matching target explicitly:

```sh
export DEPLOY_SUBSCRIPTION_ID="<subscription-guid>"
export DEPLOY_TENANT_ID="<tenant-guid>"
python3 deployment/deploy.py --help
```

The script checks these IDs against `az account show` and injects them after the
environment file. Leave the placeholder subscription/tenant IDs in the checked-in
example file unchanged.

Every stage needs `--environment ENV --tfvars PATH`. The first initialization binds
the environment to its target, backend, and exact tfvars path. Reuse that same path
for every later stage. The existing `dev` environment is bound to
`infra/environments/example.tfvars`; do not substitute another file or reinitialize
it to bypass a refusal.

For a new environment, follow the helper's initialization and foundation stages,
then the identity/bootstrap sequence in [deployment/ACA.md](deployment/ACA.md).
Check quota for all six model deployments and any retained legacy deployment;
historical quota observations in that guide are not current availability.

### Update an Existing Environment

Run each command separately, reviewing its result before proceeding:

```sh
python3 deployment/deploy.py build-images --environment dev --tfvars infra/environments/example.tfvars
python3 deployment/deploy.py plan-workloads --environment dev --tfvars infra/environments/example.tfvars
python3 deployment/deploy.py apply --stage workloads --plan <saved-plan.bin> --environment dev --tfvars infra/environments/example.tfvars
python3 deployment/deploy.py route-agents --environment dev --tfvars infra/environments/example.tfvars
python3 deployment/deploy.py verify --environment dev --tfvars infra/environments/example.tfvars
```

- Build/push, apply, and routing each require their own interactive approval:
  `BUILD AND PUSH <sha256>`, `APPROVE <sha256>`, and `ROUTE <sha256>`.
- Review the saved plan before approving. An app-only change should not alter
  models, Search, RBAC, or foundation resources. Stop and investigate unexpected changes.
- Shared instruction changes affect managed prompt definitions and the images
  used by hosted, ACA, and web containers. New Foundry versions require routing.
- For web-only updates, skip foundation, model-capacity, ACA bootstrap, and routing
  when agent versions are unchanged. Still review the actual image/resource diff.
- Never toggle `deploy_workloads=false` to restage a live environment: it can
  delete workloads. Never commit private tfvars, state, or deployment artifacts.
- `/health` and deployment verification establish readiness, not successful
  inference or Search authorization. Finish with a fresh six-way book comparison,
  inspect citations/tool evidence, and test follow-up and reset behavior.

## Catalog and Interpretation Limits

The bundled dataset contains 10,000 book records from Azure AI Search sample data.
[data/source.json](data/source.json) records the pinned upstream source and SHA256.
No license has been established for this CSV; review its provenance and permitted
use before redistributing it. Ingestion is separate from deployment; consult
[deployment/import_books.py](deployment/import_books.py) and its `--help` before
approving uploads to an existing index.

The index stores `id`, `title`, `content`, and `url`. Author names, ratings, and
other metadata are labeled text within `content`, not dedicated sortable fields.
Shared settings use lexical `simple` search with `top_k=5` and a 4,096 output-token
limit. The prompt separates author retrieval from numeric ordering, retries author
queries without middle initials when needed, and verifies authors in returned records.

Example question: **Order books by ratings that were written by George RR Martin?**

Ratings order applies only to retrieved matches, not the author's complete
bibliography or the global top-rated books. The catalog does not support claims
about plot, genre, themes, availability, or editorial reviews. A failed retrieval
does not establish that a book is absent from the index.

Equal settings improve comparability but do not guarantee deterministic output.
Latency includes hosting and downstream work; an outlier is not by itself proof
of a cold start. Missing native tool-call evidence does not mean zero tool calls,
and exposed tool items do not provide reliable per-tool durations.

For a specific run, use its comparison ID in Log Analytics:

```kusto
AppDependencies
| where tostring(Properties["comparison.id"]) == "<comparison-id>"
| project TimeGenerated, Name, OperationId, Properties
```