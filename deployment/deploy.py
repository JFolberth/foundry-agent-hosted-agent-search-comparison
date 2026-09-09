#!/usr/bin/env python3
"""Separately approved, saved-plan deployment. Python 3 standard library only."""

import argparse
import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
ARTIFACTS = ROOT / "deployment" / ".artifacts"
GUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DOCKERIGNORE = """**
!pyproject.toml
!uv.lock
!config/
config/**
!config/agent.json
!src/
!src/**
!containers/
containers/**
!containers/*.Dockerfile
**/.git
**/.venv
**/__pycache__
**/*.py[cod]
**/*.tfstate*
**/*.tfvars
**/*.tfvars.json
**/.env*
**/*.pem
**/*.key
.git
.venv
infra
deployment
"""
REPO_DOCKERIGNORE = """**
!pyproject.toml
!uv.lock
!config/
!config/agent.json
!containers/
!containers/*.Dockerfile
!src/
!src/comparison/
!src/comparison/**
!src/hosted_agent/
!src/hosted_agent/**
!src/web/
!src/web/**
**/__pycache__/
**/*.pyc
**/.env*
"""
HELP = """
SETUP AND APPROVAL BOUNDARIES
  Python 3 (stdlib), Terraform with the existing provider lock file, Azure CLI
  already signed in to the requested Azure PUBLIC-cloud subscription/tenant,
  and Docker with buildx on a local Docker Engine/Desktop are prerequisites.
  This script installs no tools, never selects an Azure account, and never
  plans during --help. Run it with Python on Windows or directly on POSIX.

  Supply requested IDs privately in DEPLOY_SUBSCRIPTION_ID and DEPLOY_TENANT_ID,
  or answer hidden terminal prompts. Every cloud-capable operation first
  compares az account show with BOTH IDs. No IDs are accepted from defaults.
  Use --environment dev|test|prod and an explicit --tfvars environment file.
  Relative input paths resolve from the repository, NOT the calling directory.
  Target/environment overrides are passed LAST; stage/image variables cannot
  accidentally be overridden by the environment file.

SEPARATELY EXECUTED FLOW (append --environment ENV --tfvars PATH to each)
  1. init --local-development
     OR init --backend-config PATH for an already configured azurerm backend.
     Normal init initializes the backend, with -lockfile=readonly. The local
     backend is explicitly single-operator DEVELOPMENT BOOTSTRAP ONLY. Its state
     goes under deployment/.artifacts/dev; existing infra state is refused.
     Configure remote state and its RBAC separately before shared/prod use.
     No backend creation, migration, -reconfigure, or -migrate-state is provided.
     Provider-only init -backend=false is for validation, NOT deployment setup.
     If the checked-in lock needs another platform, its owner must update it.
  2. plan-foundation
     Explicitly runs plan with deploy_workloads=false; refuses when state says
     workloads are enabled (or a nonempty legacy state lacks that output).
     Prints terraform show of the ACTUAL saved binary plan, target and SHA256.
     This command NEVER applies anything.
  3. apply --stage foundation --plan deployment/.artifacts/plans/PLAN/plan.bin
     Rechecks target, backend, stage, inputs and nonempty changes; shows that
     exact saved plan. A terminal must type the literal APPROVE <sha256>.
     The hash is recomputed before apply of ONLY that binary. There is no --yes.
  4. build-images
     Checks foundation outputs, an accepted root .dockerignore (below), and
     rejects sensitive filenames/symlinks in allowlisted source trees.
     Requires a SEPARATE terminal confirmation BUILD AND PUSH <source-sha256>.
     Uses containers/hosted.Dockerfile and containers/web.Dockerfile with the
     repository ROOT context and local docker buildx --platform linux/amd64
     --push. Review Python 3.11 Dockerfiles, dependencies and baked-in
     config/agent.json first. Remote builders/ACR Tasks are intentionally NOT
     used. Both images receive unique UTC/source-hash/random tags, never latest.
     Captured successful build metadata supplies real shared-ACR image digests.
     Writes deployment/.artifacts/images.tfvars.json plus provenance; neither
     is auto-loaded by Terraform. A partial build never publishes new inputs.
  5. plan-workloads
     Checks source/config hashes and registry/target against image provenance;
     explicitly passes images.tfvars.json and deploy_workloads=true.
     Requires applied foundation state (service + RBAC, no Search index yet).
     Before plan, read-only Search indexes and Foundry agents collection GETs
     must return 200; named index/agent GETs may return 200 or 404 only AFTER
     that collection check. Retries 403 propagation up to --timeout (600s).
     A fresh audience-specific CLI token is requested for each operation;
     401 fails with reauthentication guidance. Other failures stop the stage.
     No permission grants, registration, state refresh, or resources are created
     by this preflight. Successful reads do not establish write authorization.
     Workload plans must leave all foundations unchanged, including parent
     accounts/projects, Search service, model and RBAC. Any such change requires
     separately approved foundation restaging and fresh permission checks.
     Never switch deploy_workloads=false in live workload state to restage:
     it would delete workloads. Such foundation changes need separate review.
     Defaults in the root: prompt_agent_name=search-prompt,
     hosted_agent_name=search-hosted. Dedicated endpoint selectors pin versions;
     UI version environment variables can validate/report desired targets.
     The infra owner must expose deploy_workloads, prompt_agent_name/version,
     hosted_agent_name/version/identity_principal_id, and ui_url, and accept
     nullable hosted_image/web_image inputs (full shared-ACR digest references).
     A foundation-only root that forbids deploy_workloads=true must be updated
     by its owner before this stage; this script never edits infrastructure.
  6. apply --stage workloads --plan deployment/.artifacts/plans/PLAN/plan.bin
     A NEW, separate APPROVE <sha256> is mandatory.
     Validates known agent definitions from the saved plan, not nonexistent
     foundation agent state. Routing later uses only applied resource bodies
     and cross-checks root versions against each resource's exported version.
     Repeats the read-only permission gate immediately before applying the
     approved binary, then rechecks the approval's input hashes.
  7. route-agents
     SEPARATELY APPROVED, NON-PROVISIONER routing of EXISTING agents only.
     AzAPI 2.12 registers agents with POST but lacks data-plane PATCH support:
     this approved script, NOT Terraform or a third agent, manages routing.
     GETs BOTH exact Terraform-returned prompt/hosted versions; refuses unless
     both are active and both agent objects have state=enabled. Disabled agents
     are refused; this script never enables them. If pending, wait and retry.
     Requires project_id in the requested subscription and an authoritative
     ARM project GET proving project_endpoint belongs to that project.
     Exact-version managed definitions must match APPLIED Terraform resource
     bodies, including immutable image digest, protocol, CPU/memory, runtime
     environment, model, instructions, reasoning and native Search settings.
     Current unapplied files and mutable versions.latest definitions are NOT
     the approval source. Service defaults/read-only fields are not compared.
     GETs both agents and shows sanitized managed-field current -> desired diffs:
     one FixedRatio rule, exact output version, 100% traffic, responses enabled.
     Saves a private proposal under deployment/.artifacts/routes/UUID, bound
     by SHA256 to target subscription/tenant, project, names/versions, applied
     managed definitions and routes. These private definitions may be sensitive.
     A terminal must separately type ROUTE <sha256>; Terraform approval does
     NOT authorize routing. Rechecks account, state outputs, readiness and BOTH
     current routes, enabled states, ARM project binding and exact definitions
     before EVERY PATCH. Changed inputs require a new proposal.
     PATCH /agents/{name}?api-version=v1 uses application/merge-patch+json;
     reGET verifies each change and records private results. No automatic retry
     or rollback: after partial/uncertain success, rerun for remaining diffs.
     Matching first-deployment defaults require no approval or PATCH.
     CURRENT invocation: get_openai_client(agent_name=..., allow_preview=True)
     uses /agents/{name}/endpoint/protocols/openai. There is NO SDK agent_version
     argument or invented version URL/header. Shared-project agent_reference
     invocation for hosted agents is INITIAL PREVIEW, not this routing path.
     Never automatically invoked by apply, build, plan, or verify.
  8. verify [--timeout 600]
     Read-only: verifies ARM project ownership and APPLIED exact definitions,
     polls BOTH exact VERSION GETs until status=active, then requires enabled
     agent objects and actual endpoint selectors/protocols matching state. Mismatches
     fail with route-agents guidance; verify never PATCHes. Captured fresh CLI
     tokens use resource https://ai.azure.com and are never logged.
     Reads hosted instance_identity.principal_id from the actual AGENT only;
     identity may be null and is NEVER inferred or replaced by project identity.
     Then GETs public ui_url/health, if available. Empty Search index requires
     separately approved manual public-document ingestion.

SECURITY / COST
  Hosted containers use the current Responses protocol 2.0.0 and the current
  container_configuration/protocol_versions schema. Runtime must forward the
  opaque x-agent-foundry-call-id unchanged on downstream Foundry calls, scoped
  to each request. Deprecated protocol plans are refused before approval.
  https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract#platform-request-headers-container-protocol-200

  All script artifacts, Terraform data, plan files, logs, image metadata and
  isolated Docker credentials live ONLY under deployment/.artifacts (ignored).
  POSIX directories are 0700 and files 0600; on Windows chmod cannot establish
  private ACLs: use a private encrypted user directory and restrict its NTFS
  ACLs before running. Tool-owned temporary files and provider sockets use the
  operating system's temporary directory. No global Docker login is used.
  Saved plans/state contain secrets, including App Insights configuration:
  NEVER commit/share them. Output JSON and apply/build diagnostics are captured,
  not dumped to the console. Human-readable plans use Terraform's sensitive
  masking; review privately. Keep inputs quiescent while reviewing/building.
  IDs, hashes, agent identities and resource URLs are shown only intentionally.
  Protect the artifact directory from other local processes; SHA256 is an
  approval binding, not a signature against a malicious local administrator.
  Terraform and Docker may maintain their own engine/backend caches. Existing
  Azure CLI credentials remain managed by Azure CLI; tokens are not logged.

  Operator needs resource deployment permissions and role-assignment authority
  (e.g. Owner, or Contributor + RBAC Administrator), Foundry data-plane access,
  AcrPush for the shared registry, and remote-state blob access if configured.
  AzAPI automatic resource-provider registration is disabled so planning does
  not register providers. Required namespaces must already be registered:
  Microsoft.App, Microsoft.CognitiveServices, Microsoft.ContainerRegistry,
  Microsoft.Insights, Microsoft.ManagedIdentity, Microsoft.OperationalInsights,
  and Microsoft.Search. Missing registrations need separate explicit approval;
  this script does not register them.
  Runtime managed identities need the root's least-privilege role assignments;
  allow RBAC propagation. Confirm model availability/quota and preview support.
  Public endpoints are a PAID DEMO, not production security. The public UI can
  generate model costs; Search, hosted compute, ACR and Container Apps can incur
  charges while idle. Image push and each infrastructure stage need approval.
  Cleanup/destruction is a separate approval lifecycle, not implemented here.

REQUIRED ROOT .dockerignore
  The repository-owned ordered allowlist below is accepted (comments/blank
  lines allowed). Preflight permits only project/lock/config files, container
  Dockerfiles, and src/comparison, src/hosted_agent, src/web. Docker's parent
  directory negations can include other descendants, so unexpected entries
  directly under src, config, or containers are REFUSED, not silently skipped
  (except ignored caches/.env*). This script never edits .dockerignore.
  Dockerfile-specific .dockerignore overrides are rejected.
  Preflight rejects symlinks, .git/.venv entries, .pem/.key files and Terraform
  state/variable filenames in included source trees, rather than silently
  omitting them from provenance while Docker could send them. Ignored caches
  and .env* entries are not fingerprinted. All remaining context files ARE
  fingerprinted. Filename checks cannot detect secrets embedded in ordinary
  code/config files: review these yourself and keep build inputs quiescent.
""" + "\n".join("  " + line for line in REPO_DOCKERIGNORE.splitlines()) + """

  The original ordered allowlist is also accepted, with the same preflight
  rejection checks. Unlike the narrower rules above, it includes ALL src trees:
""" + "\n".join("  " + line for line in DOCKERIGNORE.splitlines()) + """

REFERENCES
  https://learn.microsoft.com/cli/azure/account
  https://learn.microsoft.com/azure/container-registry/container-registry-authentication
  https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent
  https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent#configure-agent-endpoint-routing
  https://learn.microsoft.com/rest/api/searchservice/indexes/list?view=rest-searchservice-2024-07-01
  https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2026-03-01/accounts/projects
  https://github.com/Azure/terraform-provider-azapi/blob/v2.12.0/internal/services/azapi_data_plane_resource.go
  https://developer.hashicorp.com/terraform/cli/commands/plan
  https://developer.hashicorp.com/terraform/cli/commands/apply
"""


class DeploymentError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    """Reject symlinks in every path component before accessing private inputs."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), f"Symlinks are not permitted: {part}")
    require(path.is_file(), f"Required regular file missing: {path}")
    require(stat.S_ISREG(path.stat().st_mode), f"Not a regular file: {path}")
    return path


def private_dir(path):
    require(path == ARTIFACTS or ARTIFACTS in path.parents,
            "Artifacts must remain under deployment/.artifacts.")
    require(not path.is_symlink(), "Artifact symlinks are forbidden.")
    if path != ARTIFACTS:
        private_dir(path.parent)
    else:
        require(not path.parent.is_symlink(), "Deployment directory cannot be a symlink.")
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path, data):
    private_dir(path.parent)
    require(not path.is_symlink(), "Artifact symlinks are forbidden.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        path.chmod(0o600)
        stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def write_json(path, data):
    write_private(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path):
    return json.loads(regular(path).read_text(encoding="utf-8"))


def input_path(value):
    path = Path(value).expanduser()
    return regular(path if path.is_absolute() else ROOT / path)


def executable(name):
    found = shutil.which(name)
    require(found, f"Required existing tool not found: {name}")
    if os.name == "nt" and name == "az" and Path(found).suffix.lower() in (".cmd", ".bat"):
        # Invoke the CLI's bundled Python directly, not cmd.exe or a batch shell.
        python = Path(found).parent.parent / "python.exe"
        require(python.is_file(), "Cannot find Azure CLI bundled python.exe; use its supported installation.")
        return [str(python), "-I", "-m", "azure.cli"]
    require(Path(found).suffix.lower() not in (".cmd", ".bat"),
            f"{name} must be an executable, not a shell wrapper.")
    return [found]


def command(argv, env=None, stdin=None, log=None, accepted=(0,), timeout=None):
    result = subprocess.run(argv, cwd=ARTIFACTS, env=env, input=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=timeout)
    if log:
        write_private(log, result.stdout + b"\n" + result.stderr)
    require(result.returncode in accepted,
            f"{Path(argv[0]).name} failed (exit {result.returncode})."
            + (f" Private diagnostics: {log}" if log else " Output withheld to protect secrets."))
    return result


def confirm(literal):
    require(sys.stdin.isatty() and sys.stdout.isatty(),
            "Approval requires an interactive terminal; piping approval is forbidden.")
    require(input(f"Type exactly {literal}\n> ") == literal, "Not approved; nothing executed.")


def private_id(name):
    value = os.environ.get(name)
    if not value:
        require(sys.stdin.isatty(), f"Supply {name} privately or use a terminal.")
        value = getpass.getpass(f"{name}: ")
    value = value.strip().lower()
    require(GUID.fullmatch(value) and uuid.UUID(value).int, f"{name} must be a real nonzero UUID.")
    return value


class Session:
    def __init__(self, args):
        self.args = args
        self.target = {
            "subscription_id": private_id("DEPLOY_SUBSCRIPTION_ID"),
            "tenant_id": private_id("DEPLOY_TENANT_ID"),
            "environment": args.environment,
        }
        self.tfvars = input_path(args.tfvars)
        self.home = private_dir(ARTIFACTS / args.environment)
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith(("TF_", "ARM_")):
                del self.env[key]
        self.env.update({
            "TF_DATA_DIR": str(private_dir(self.home / "terraform")),
            "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "TF_WORKSPACE": "default",
            "ARM_SUBSCRIPTION_ID": self.target["subscription_id"],
            "ARM_TENANT_ID": self.target["tenant_id"],
            "ARM_USE_CLI": "true", "ARM_USE_MSI": "false", "ARM_USE_OIDC": "false",
            "CHECKPOINT_DISABLE": "1",
        })
        # Preserve OS temp paths: long repo paths exceed Unix provider socket limits.

    def account(self, timeout=None):
        raw = command(executable("az") + ["account", "show", "--output", "json",
                                         "--only-show-errors"], env=self.env, timeout=timeout).stdout
        data = json.loads(raw)
        require(str(data.get("id", "")).lower() == self.target["subscription_id"]
                and str(data.get("tenantId", "")).lower() == self.target["tenant_id"],
                "Azure CLI subscription/tenant mismatch. Select the intended account yourself; refusing.")
        require(data.get("state") == "Enabled" and data.get("environmentName") == "AzureCloud",
                "An enabled Azure public-cloud subscription is required.")

    def show_target(self):
        print("Target:", json.dumps(self.target, sort_keys=True))
        print("Environment file:", self.tfvars)

    def tf(self, *args, log=None, accepted=(0,)):
        self.account()
        return command(executable("terraform") + [f"-chdir={INFRA}", *args],
                       env=self.env, log=log, accepted=accepted)

    def binding(self):
        backend = regular(INFRA / "backend.tf")
        return {"target": self.target, "tfvars": str(self.tfvars),
                "backend_sha256": digest(backend.read_bytes())}

    def initialized(self):
        record = read_json(self.home / "initialized.json")
        require(record["binding"] == self.binding(),
                "Backend/target/environment file changed; use the original initialized environment.")
        if record.get("backend_config"):
            path = input_path(record["backend_config"])
            require(digest(path.read_bytes()) == record["backend_config_sha256"],
                    "Backend config changed; migration/reconfiguration needs separate approval.")
        require(digest(regular(self.home / "terraform" / "terraform.tfstate").read_bytes())
                == record["backend_metadata_sha256"],
                "Initialized backend metadata changed; refusing a different state target.")

    def outputs(self):
        self.initialized()
        data = json.loads(self.tf("output", "-json").stdout)
        return {key: item["value"] for key, item in data.items()}

    def state(self):
        self.initialized()
        return json.loads(self.tf("show", "-json").stdout).get("values", {})

    def foundation_safe(self, outputs):
        state = json.loads(self.tf("show", "-json").stdout)
        foundation_guard(outputs, state_has_resources(state.get("values", {})))

    def registry(self, outputs):
        server = outputs.get("registry_login_server", "")
        name = outputs.get("registry_name", "")
        arm_id = outputs.get("registry_id", "")
        require(re.fullmatch(r"[a-zA-Z0-9]{5,50}", name or "")
                and re.fullmatch(r"[a-z0-9-]+\.azurecr\.io", server or ""),
                "Valid shared ACR foundation outputs are required.")
        require(arm_id.lower().startswith(f"/subscriptions/{self.target['subscription_id']}/")
                and arm_id.lower().endswith(f"/providers/microsoft.containerregistry/registries/{name.lower()}"),
                "Shared ACR state output does not match the requested subscription.")
        return name, server


def hcl_string_end(text, index):
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index
        elif text.startswith(("$${", "%%{"), index):
            index += 3
        elif text.startswith(("${", "%{"), index):
            index += 2
            depth = 1
            while index < len(text) and depth:
                if text[index] == '"':
                    index = hcl_string_end(text, index) + 1
                elif text.startswith(("#", "//"), index):
                    end = text.find("\n", index)
                    require(end >= 0, "Unterminated HCL template comment.")
                    index = end + 1
                elif text.startswith("/*", index):
                    end = text.find("*/", index + 2)
                    require(end >= 0, "Unterminated HCL template comment.")
                    index = end + 2
                else:
                    require(not text.startswith("<<", index),
                            "Heredocs inside backend-file templates are unsupported.")
                    depth += (text[index] == "{") - (text[index] == "}")
                    index += 1
            require(depth == 0, "Unterminated HCL template.")
        else:
            index += 1
    raise DeploymentError("Unterminated HCL string.")


def active_backends(text):
    """Lex HCL before looking for backend blocks; examples are not configuration."""
    tokens = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith(("#", "//"), index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            require(end >= 0, "Unterminated HCL block comment.")
            index = end + 2
        elif text[index] == '"':
            end = hcl_string_end(text, index)
            value = text[index + 1:end]
            tokens.append(("string", value))
            index = end + 1
        elif text.startswith("<<", index):
            match = re.match(r"<<-?([A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n", text[index:])
            require(match is not None, "Malformed HCL heredoc.")
            end = re.search(r"(?m)^[ \t]*" + re.escape(match[1]) + r"[ \t]*\r?$",
                            text[index + match.end():])
            require(end is not None, "Unterminated HCL heredoc.")
            index += match.end() + end.end()
            tokens.append(("string", ""))
        else:
            match = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", text[index:])
            value = match[0] if match else text[index]
            tokens.append(("token", value))
            index += len(value)
    stack, backends = [], []
    for index, token in enumerate(tokens):
        if token == ("token", "{"):
            header = tokens[max(0, index - 2):index]
            if stack == ["terraform"] and len(header) == 2 and header[0] == ("token", "backend"):
                require(header[1][0] == "string" and header[1][1] in ("local", "azurerm"),
                        "Only literal local/azurerm backends are supported.")
                backends.append(header[1][1])
            stack.append("terraform" if not stack and index > 0
                         and tokens[index - 1] == ("token", "terraform") else "other")
        elif token == ("token", "}"):
            require(stack, "Unbalanced HCL braces.")
            stack.pop()
    require(not stack, "Unbalanced HCL braces.")
    return backends


def configured_backend():
    backends = []
    for path in INFRA.glob("*.tf"):
        backends.extend(active_backends(regular(path).read_text(encoding="utf-8")))
    for path in INFRA.glob("*.tf.json"):
        configuration = read_json(path).get("terraform", {})
        blocks = configuration if isinstance(configuration, list) else [configuration]
        for block in blocks:
            backends.extend(block.get("backend", {}).keys())
    require(len(backends) == 1 and backends[0] in ("local", "azurerm"),
            "Exactly one active local/azurerm backend is required.")
    return backends[0]


def initialize(session):
    require(not (session.home / "initialized.json").exists(),
            "Already initialized. This script never migrates or reconfigures state.")
    backend = configured_backend()
    require(not (session.home / "terraform" / "terraform.tfstate").exists()
            and not (INFRA / ".terraform" / "terraform.tfstate").exists()
            and not list(INFRA.glob("*.tfstate*"))
            and not (INFRA / "terraform.tfstate.d").exists()
            and not (session.home / "state").exists(),
            "Existing state/backend metadata detected. Migration requires separate approval.")
    args = ["init", "-input=false", "-no-color", "-lockfile=readonly"]
    record = {"binding": session.binding()}
    if session.args.local_development:
        require(session.args.environment == "dev", "Local bootstrap is development only.")
        require(backend == "local",
                "--local-development requires the existing local backend.")
        require(not list(INFRA.glob("*.tfstate*")) and not (INFRA / "terraform.tfstate.d").exists(),
                "Existing infra state detected. Do not bootstrap over it; migration requires separate approval.")
        state = private_dir(session.home / "state") / "terraform.tfstate"
        args += [f"-backend-config=path={state}",
                 f"-backend-config=workspace_dir={state.parent / 'workspaces'}"]
    else:
        require(backend == "azurerm",
                "Have the infra owner configure the azurerm backend before remote init.")
        config = input_path(session.args.backend_config)
        record.update(backend_config=str(config), backend_config_sha256=digest(config.read_bytes()))
        args.append(f"-backend-config={config}")
    regular(INFRA / ".terraform.lock.hcl")
    session.show_target()
    session.tf(*args, log=session.home / "init.log")
    metadata = regular(session.home / "terraform" / "terraform.tfstate")
    record["backend_metadata_sha256"] = digest(metadata.read_bytes())
    write_json(session.home / "initialized.json", record)
    print("Backend initialized. No plan or apply performed.")


def state_has_resources(values):
    def populated(module):
        return bool(module.get("resources")) or any(
            populated(child) for child in module.get("child_modules", []))
    return populated(values.get("root_module", {}))


def foundation_guard(outputs, has_resources=False):
    require((not outputs and not has_resources) or outputs.get("deploy_workloads") is False,
            "Foundation stage refused: workloads are enabled, or existing state lacks deploy_workloads=false.")


def fingerprint(paths):
    result = {}
    for path in sorted(set(paths)):
        path = regular(path)
        result[path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
    return result


def infra_inputs():
    paths = [INFRA / ".terraform.lock.hcl", ROOT / "config" / "agent.json"]
    for base, dirs, files in os.walk(INFRA, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".terraform"]
        for directory in dirs:
            require(not (Path(base) / directory).is_symlink(), "Infrastructure symlinks are forbidden.")
        paths += [Path(base) / f for f in files if f.endswith((".tf", ".tf.json"))]
    return fingerprint(paths)


def build_inputs():
    ignore = regular(ROOT / ".dockerignore")
    rules = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    narrow = rules == REPO_DOCKERIGNORE.splitlines()
    require(narrow or rules == DOCKERIGNORE.splitlines(),
            "Root .dockerignore must match one of the ordered allowlists in --help; ask its owner to review it.")
    require(not list((ROOT / "containers").glob("*.dockerignore")),
            "Dockerfile-specific ignore files override root protection and are forbidden.")
    paths = [ignore, ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / "config" / "agent.json"]
    paths += list((ROOT / "containers").glob("*.Dockerfile"))
    for name in ("hosted", "web"):
        path = regular(ROOT / "containers" / f"{name}.Dockerfile")
        require(re.search(r"(?im)^FROM\s+python:3\.11[-@\s]", path.read_text(encoding="utf-8")),
                f"{name}.Dockerfile must use Python 3.11.")
    require((ROOT / "src").is_dir() and not (ROOT / "src").is_symlink(), "A real src directory is required.")
    roots = [ROOT / "src"]
    if narrow:
        # Parent negations also include descendants; enforce the intended boundary.
        for directory, allowed in (
            ("config", {"agent.json"}),
            ("containers", set()),
            ("src", {"comparison", "hosted_agent", "web"}),
        ):
            for path in (ROOT / directory).iterdir():
                require(not path.is_symlink(), f"Symlinks forbidden in build inputs: {path}")
                if (path.name.startswith(".env") or path.name == "__pycache__"
                        or path.name.endswith(".pyc")):
                    continue
                require(path.name in allowed
                        or (directory == "containers" and path.name.endswith(".Dockerfile")),
                        f"Unexpected entry would widen the build context: {path}")
        roots = [ROOT / "src" / name for name in ("comparison", "hosted_agent", "web")]

    def walk_error(error):
        raise DeploymentError("Cannot inspect build source tree; refusing an incomplete preflight.") from error

    for root in roots:
        require(not root.is_symlink(), f"Symlinks forbidden in build inputs: {root}")
        if not root.exists():
            continue
        require(root.is_dir(), f"Build source must be a directory: {root}")
        for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            excluded = set()
            for name in dirs + files:
                path = Path(base) / name
                require(not path.is_symlink(), f"Symlinks forbidden in build inputs: {path}")
                lower = name.lower()
                require(not (lower in (".git", ".venv")
                             or lower.endswith((".pem", ".key", ".tfvars", ".tfvars.json"))
                             or ".tfstate" in lower),
                        f"Sensitive filename forbidden in build inputs: {path}")
                if (name.startswith(".env")
                        or name == "__pycache__"
                        or (name.endswith(".pyc") if narrow else re.search(r"\.py[cod]$", name))):
                    excluded.add(name)
            dirs[:] = [name for name in dirs if name not in excluded]
            paths += [Path(base) / name for name in files if name not in excluded]
    return fingerprint(paths)


def source_hash(inputs):
    return digest(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode())


def image_inputs(session, outputs):
    _, registry = session.registry(outputs)
    record = read_json(ARTIFACTS / "images.provenance.json")
    values = read_json(ARTIFACTS / "images.tfvars.json")
    require(record["target"] == session.target and record["images"] == values,
            "Image provenance belongs to another target or image inputs were changed.")
    require(record["inputs"] == build_inputs(), "Build inputs/config changed. Approve fresh image builds first.")
    require(set(values) == {"hosted_image", "web_image"}, "Unexpected image variables.")
    for key, value in values.items():
        require(isinstance(value, str) and re.fullmatch(
            re.escape(registry) + r"/search-" + key.split("_")[0] + r"@sha256:[0-9a-f]{64}", value),
            "Images must be actual fully qualified digest references in the shared ACR.")
    return values, record


def changes_present(plan):
    resource_changes = [r.get("change", {}) for r in plan.get("resource_changes", [])]
    output_changes = list(plan.get("output_changes", {}).values())
    return any(c.get("actions") not in (None, [], ["no-op"])
               for c in resource_changes + output_changes)


def workload_foundation_guard(plan):
    workloads = {
        "module.search.azapi_data_plane_resource.index[0]",
        "module.workloads[0].azapi_data_plane_resource.prompt",
        "module.workloads[0].azapi_data_plane_resource.hosted",
        "module.workloads[0].azapi_resource.web",
    }
    for resource in plan.get("resource_changes", []):
        actions = resource.get("change", {}).get("actions")
        if actions == ["no-op"] or (resource.get("mode") == "data" and actions == ["read"]):
            continue
        require(resource.get("mode") == "managed" and resource.get("address") in workloads,
                "Workload plan changes foundational resources or RBAC; permission checks against "
                "old parents are insufficient. Separately approve/re-stage foundations first, "
                "then create a fresh workload plan. No automatic migration or apply is allowed.")


def validate_plan(session, plan, stage, images=None):
    require(plan.get("errored") is not True and plan.get("applyable") is not False,
            "The saved plan is not applicable.")
    require(changes_present(plan), "Empty plan: nothing to approve or apply.")
    variables = {k: v.get("value") for k, v in plan.get("variables", {}).items()}
    for key, value in session.target.items():
        require(variables.get(key) == value, f"Saved plan target mismatch: {key}")
    require(variables.get("deploy_workloads") is (stage == "workloads"), "Saved plan stage mismatch.")
    prior_values = plan.get("prior_state", {}).get("values", {})
    prior = prior_values.get("outputs", {})
    if stage == "foundation":
        foundation_guard({k: v.get("value") for k, v in prior.items()},
                         state_has_resources(prior_values))
    else:
        require(images is not None, "Workloads plan requires verified image provenance.")
        workload_foundation_guard(plan)
        for key, value in images.items():
            require(variables.get(key) == value, "Saved plan image differs from approved build.")
    planned = plan.get("planned_values", {}).get("outputs", {}).get("deploy_workloads", {})
    require(planned.get("value") is (stage == "workloads"),
            "Root must output deploy_workloads with the planned stage value.")
    modules = [plan.get("planned_values", {}).get("root_module", {})]
    while modules:
        module = modules.pop()
        modules.extend(module.get("child_modules", []))
        for resource in module.get("resources", []):
            definition = (resource.get("values", {}).get("body") or {}).get("definition", {})
            if (stage == "workloads" and resource.get("address") in (
                    "module.workloads[0].azapi_data_plane_resource.prompt",
                    "module.workloads[0].azapi_data_plane_resource.hosted") and definition):
                kind = resource["address"].rsplit(".", 1)[1]
                require(managed_definition(definition, kind) == definition,
                        "Saved plan contains unsupported managed definition fields; review the verifier first.")
            if definition.get("kind") == "hosted":
                require(
                    definition.get("protocol_versions") == [
                        {"protocol": "responses", "version": "2.0.0"}
                    ],
                    "Hosted plans must use current Responses protocol 2.0.0. "
                    "Rebuild compatible images and create a fresh plan; "
                    "deprecated or legacy protocol configuration cannot be approved.",
                )


def show_plan(session, binary):
    # JSON may include unredacted secrets: keep it in memory, never dump it.
    plan = json.loads(session.tf("show", "-json", str(binary)).stdout)
    text = session.tf("show", "-no-color", str(binary)).stdout
    return plan, text


def plan_stage(session, stage):
    outputs = session.outputs()
    images, provenance = (None, None)
    if stage == "foundation":
        session.foundation_safe(outputs)
    else:
        images, provenance = image_inputs(session, outputs)
        permission_readiness(session, timeout=getattr(session.args, "timeout", 600))
    inputs = infra_inputs()
    tfvars_hash = digest(session.tfvars.read_bytes())
    folder = private_dir(ARTIFACTS / "plans" / f"{stage}-{uuid.uuid4().hex}")
    binary = folder / "plan.bin"
    values = {**session.target, "deploy_workloads": stage == "workloads"}
    if stage == "foundation":
        values.update(hosted_image=None, web_image=None)
    overrides = folder / "stage.tfvars.json"
    write_json(overrides, values)
    args = ["plan", "-input=false", "-no-color", "-lock-timeout=60s",
            "-detailed-exitcode", f"-var-file={session.tfvars}"]
    if images:
        args.append(f"-var-file={ARTIFACTS / 'images.tfvars.json'}")
    args += [f"-var-file={overrides}", f"-out={binary}"]
    session.tf(*args, log=folder / "plan.log", accepted=(0, 2))
    regular(binary).chmod(0o600)
    plan, text = show_plan(session, binary)
    validate_plan(session, plan, stage, images)
    require(inputs == infra_inputs() and tfvars_hash == digest(session.tfvars.read_bytes()),
            "Inputs changed during planning; discard this plan and explicitly plan again.")
    if images:
        require(image_inputs(session, session.outputs()) == (images, provenance),
                "Image provenance changed during planning.")
    sha = digest(binary.read_bytes())
    write_private(folder / "plan.txt", text)
    write_json(folder / "approval.json", {
        "sha256": sha, "stage": stage, "binding": session.binding(),
        "tfvars_sha256": tfvars_hash, "infra_inputs": inputs,
        "images": images, "provenance": provenance,
    })
    session.show_target()
    print(text.decode("utf-8", errors="replace"))
    print(f"Saved binary: {binary}\nStage: {stage}\nSHA256: {sha}")
    print("No apply performed. Review privately, then execute the separate apply subcommand.")


def apply_stage(session):
    binary = input_path(session.args.plan)
    require(ARTIFACTS / "plans" in binary.parents and binary.name == "plan.bin",
            "Only a saved deployment/.artifacts/plans/.../plan.bin can be applied.")
    record = read_json(binary.parent / "approval.json")
    require(not (binary.parent / "applied.json").exists(), "This plan was already applied.")
    stage = session.args.stage
    require(record["stage"] == stage and record["binding"] == session.binding(),
            "Saved plan stage/target/backend binding mismatch.")
    outputs = session.outputs()
    if stage == "foundation":
        session.foundation_safe(outputs)
    images = None
    if stage == "workloads":
        images, provenance = image_inputs(session, outputs)
        require(images == record["images"] and provenance == record["provenance"],
                "Saved plan does not match current approved image provenance.")

    def unchanged():
        session.initialized()
        require(digest(regular(binary).read_bytes()) == record["sha256"], "Saved binary plan hash changed.")
        require(record["tfvars_sha256"] == digest(session.tfvars.read_bytes())
                and record["infra_inputs"] == infra_inputs(), "Inputs changed; explicitly create a new plan.")
        if stage == "workloads":
            require(image_inputs(session, outputs) == (record["images"], record["provenance"]),
                    "Image inputs changed after plan approval.")

    unchanged()
    plan, text = show_plan(session, binary)
    validate_plan(session, plan, stage, images)
    unchanged()
    session.show_target()
    print(text.decode("utf-8", errors="replace"))
    print(f"Stage: {stage}\nSaved binary: {binary}\nSHA256: {record['sha256']}")
    confirm(f"APPROVE {record['sha256']}")
    session.account()
    unchanged()
    if stage == "foundation":
        session.foundation_safe(session.outputs())
    else:
        permission_readiness(session, timeout=getattr(session.args, "timeout", 600))
        unchanged()
    session.tf("apply", "-input=false", "-no-color", "-lock-timeout=60s", str(binary),
               log=binary.parent / "apply.log")
    write_json(binary.parent / "applied.json", {"sha256": record["sha256"], "stage": stage})
    print("Exact saved plan applied. Output/logs remain private. Readiness is checked separately with verify.")


def buildx_plugin_directory(plugins):
    require(isinstance(plugins, list) and all(isinstance(item, dict) for item in plugins),
            "Docker returned invalid CLI plugin metadata.")
    matches = [item for item in plugins if item.get("Name") == "buildx"]
    require(len(matches) == 1 and not matches[0].get("Err"),
            "A working, installed Docker Buildx plugin is required.")
    value = matches[0].get("Path")
    require(isinstance(value, str) and value, "Docker did not report the Buildx plugin path.")
    path = Path(value)
    require(path.is_absolute() and path.name in ("docker-buildx", "docker-buildx.exe")
            and path.is_file() and os.access(path, os.X_OK),
            "Docker's Buildx plugin must be an existing executable at an absolute path.")
    return str(path.parent)


def build_images(session):
    outputs = session.outputs()
    name, registry = session.registry(outputs)
    inputs = build_inputs()
    sha = source_hash(inputs)
    docker = executable("docker")
    contexts = json.loads(command(docker + ["context", "inspect"], env=session.env).stdout)
    host = contexts[0]["Endpoints"]["docker"]["Host"]
    require(host.startswith(("unix:///", "npipe:////./pipe/")),
            "Only a local Docker Engine/Desktop socket is permitted; remote context refused.")
    plugins = json.loads(command(
        docker + ["info", "--format", "{{json .ClientInfo.Plugins}}"], env=session.env).stdout)
    plugin_directory = buildx_plugin_directory(plugins)
    session.show_target()
    print(f"Build and PUSH BOTH linux/amd64 images to {registry}; source SHA256: {sha}")
    print(f"Repository-root context: {ROOT}")
    confirm(f"BUILD AND PUSH {sha}")
    session.account()
    require(inputs == build_inputs(), "Source changed during approval; restart build-images.")
    folder = private_dir(ARTIFACTS / "builds" / uuid.uuid4().hex)
    config = private_dir(folder / "docker")
    # Keep plugin discovery without copying the user's registry credentials.
    write_json(config / "config.json", {"cliPluginsExtraDirs": [plugin_directory]})
    env = session.env.copy()
    for key in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                "BUILDX_BUILDER", "BUILDX_CONFIG"):
        env.pop(key, None)
    env["DOCKER_CONFIG"] = str(config)
    env["BUILDX_CONFIG"] = str(private_dir(folder / "buildx"))
    docker += ["--host", host, "--config", str(config)]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{stamp}-{sha[:16]}-{uuid.uuid4().hex}"
    images = {}
    try:
        session.account()
        token = json.loads(command(executable("az") + [
            "acr", "login", "--name", name, "--subscription", session.target["subscription_id"],
            "--expose-token", "--output", "json", "--only-show-errors"], env=session.env).stdout)
        require(token.get("loginServer") == registry and token.get("accessToken"),
                "ACR authentication response does not match the shared registry.")
        command(docker + ["login", registry, "--username", "00000000-0000-0000-0000-000000000000",
                          "--password-stdin"], env=env, stdin=token["accessToken"].encode())
        del token
        for kind in ("hosted", "web"):
            session.account()
            require(inputs == build_inputs(), "Sources changed between builds; refusing mismatched images.")
            metadata = folder / f"{kind}.metadata.json"
            image = f"{registry}/search-{kind}:{tag}"
            command(docker + ["buildx", "build", "--builder", "default",
                              "--platform", "linux/amd64", "--push", "--provenance=false",
                              "--sbom=false", "--metadata-file", str(metadata),
                              "--file", str(ROOT / "containers" / f"{kind}.Dockerfile"),
                              "--tag", image, str(ROOT)],
                    env=env, log=folder / f"{kind}.build.log")
            metadata.chmod(0o600)
            actual_digest = read_json(metadata).get("containerimage.digest", "")
            require(DIGEST.fullmatch(actual_digest), "Successful build did not return a real image digest.")
            images[f"{kind}_image"] = f"{registry}/search-{kind}@{actual_digest}"
        require(inputs == build_inputs(), "Sources changed during builds; rebuild both images.")
        write_json(ARTIFACTS / "images.tfvars.json", images)
        write_json(ARTIFACTS / "images.provenance.json", {
            "target": session.target, "inputs": inputs, "source_sha256": sha,
            "config_sha256": inputs["config/agent.json"], "images": images,
            "tag": tag, "metadata_directory": str(folder),
        })
    finally:
        # Never leave a registry access token in Docker's isolated config.
        credential_file = config / "config.json"
        if credential_file.exists():
            regular(credential_file).unlink()
    print("Both images pushed; actual digest tfvars and provenance saved privately. No plan/apply performed.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_url(value, suffix):
    require(isinstance(value, str), "Missing public service URL output.")
    url = urllib.parse.urlsplit(value)
    require(url.scheme == "https" and url.hostname and url.hostname.endswith(suffix)
            and not url.username and not url.password and url.port in (None, 443)
            and not url.query and not url.fragment, "Untrusted service endpoint; refusing HTTP request.")
    return value.rstrip("/")


def project_target(outputs, target):
    project_id = outputs.get("project_id")
    require(isinstance(project_id, str) and re.fullmatch(
        r"/subscriptions/" + re.escape(target["subscription_id"])
        + r"/resourceGroups/[a-zA-Z0-9_.()-]+/providers/Microsoft\.CognitiveServices"
          r"/accounts/[a-zA-Z0-9_-]+/projects/[a-zA-Z0-9_-]+", project_id, re.I),
        "Project ARM ID must belong to the requested subscription.")
    endpoint = public_url(outputs.get("project_endpoint"), ".services.ai.azure.com")
    parsed = urllib.parse.urlsplit(endpoint)
    require(not any(character.isspace() or ord(character) < 32 for character in endpoint)
            and re.fullmatch(r"[a-zA-Z0-9-]+\.services\.ai\.azure\.com", parsed.hostname)
            and re.fullmatch(r"/api/projects/[a-zA-Z0-9_-]+", parsed.path),
            "Unexpected Foundry project endpoint host/path.")
    return {"project_id": project_id, "project_endpoint": endpoint}


def confirm_project(session, target, timeout=30):
    bound = project_target(target, session.target)
    # Never trust data-plane resource IDs as evidence of subscription ownership.
    project = azure_request(session, "https://management.azure.com" + bound["project_id"]
                            + "?api-version=2026-03-01", "https://management.azure.com/",
                            timeout=timeout)
    require(isinstance(project.get("id"), str)
            and project["id"].lower() == bound["project_id"].lower(),
            "ARM returned a different project.")
    endpoint = project.get("properties", {}).get("endpoints", {}).get("AI Foundry API")
    require(endpoint is not None and project_target(
        {"project_id": bound["project_id"], "project_endpoint": endpoint}, session.target) == bound,
        "State endpoint does not belong to the authoritative ARM project.")


def routing_targets(outputs, target):
    require(outputs.get("deploy_workloads") is True, "Workloads are not enabled in state.")
    project = project_target(outputs, target)
    agents = {}
    for kind in ("prompt", "hosted"):
        name, version = outputs.get(f"{kind}_agent_name"), outputs.get(f"{kind}_agent_version")
        for value in (name, version):
            require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*", value),
                    f"Missing/malformed exact {kind} agent name/version Terraform outputs.")
        agents[kind] = {"name": name, "version": version}
    require(agents["prompt"]["name"] != agents["hosted"]["name"],
            "Prompt and hosted outputs must identify two distinct existing agents.")
    return {**project, "deploy_workloads": True, "agents": agents}


def managed_definition(definition, kind):
    """Typed projection of the schema this root manages, not service defaults."""
    require(isinstance(definition, dict) and definition.get("kind") == kind,
            "Missing/wrong agent definition kind.")

    def project(value, schema):
        require(isinstance(value, dict), "Malformed managed agent definition object.")
        result = {}
        for key, expected_type in schema.items():
            item = value.get(key)
            if isinstance(expected_type, dict):
                result[key] = project(item, expected_type)
            else:
                require(type(item) is expected_type, f"Malformed managed definition field: {key}")
                if expected_type is str:
                    require(bool(item), f"Empty managed definition field: {key}")
                result[key] = item
        return result

    if kind == "prompt":
        result = project(definition, {"kind": str, "model": str, "instructions": str,
                                     "reasoning": {"effort": str}, "tools": list})
        require(result["reasoning"]["effort"] == "low", "Prompt reasoning effort must be low.")
        require(len(result["tools"]) == 1, "Exactly one approved native Search tool is required.")
        tool = project(result["tools"][0], {"type": str, "azure_ai_search": {"indexes": list}})
        require(tool["type"] == "azure_ai_search" and len(tool["azure_ai_search"]["indexes"]) == 1,
                "Exactly one native Search index is required.")
        index = project(tool["azure_ai_search"]["indexes"][0], {
            "project_connection_id": str, "index_name": str, "query_type": str, "top_k": int})
        require(index["query_type"] == "simple" and index["top_k"] > 0,
                "Invalid native Search query settings.")
        tool["azure_ai_search"]["indexes"] = [index]
        result["tools"] = [tool]
        return result
    require(kind == "hosted", "Unsupported agent kind.")
    result = project(definition, {"kind": str, "container_configuration": {"image": str},
                                 "cpu": str, "memory": str, "protocol_versions": list,
                                 "environment_variables": dict})
    require(re.fullmatch(r"[a-z0-9-]+\.azurecr\.io/[a-z0-9/_.-]+@sha256:[0-9a-f]{64}",
                         result["container_configuration"]["image"]),
            "Hosted image must be an immutable ACR digest.")
    result["protocol_versions"] = [project(protocol, {"protocol": str, "version": str})
                                   for protocol in result["protocol_versions"]]
    require(result["protocol_versions"] == [{"protocol": "responses", "version": "2.0.0"}],
            "Hosted version must implement Responses protocol 2.0.0.")
    environment = result["environment_variables"]
    require(all(type(key) is str and type(value) is str for key, value in environment.items())
            and all(environment.get(key) for key in (
                "HOSTED_AGENT_NAME", "PROMPT_AGENT_NAME", "MODEL_DEPLOYMENT_NAME",
                "SEARCH_INDEX_NAME", "SEARCH_PROJECT_CONNECTION_ID")),
            "Missing/malformed hosted runtime environment.")
    return result


def state_resources(values):
    modules = [values.get("root_module", {})]
    resources = {}
    while modules:
        module = modules.pop()
        modules.extend(module.get("child_modules", []))
        for resource in module.get("resources", []):
            resources[resource["address"]] = resource
    return resources


def applied_targets(session):
    values = session.state()  # terraform show reads applied state; never refresh/plan here.
    outputs = {key: item["value"] for key, item in values.get("outputs", {}).items()}
    targets = routing_targets(outputs, session.target)
    resources = state_resources(values)
    definitions = {}
    for kind, agent in targets["agents"].items():
        resource = resources.get(f"module.workloads[0].azapi_data_plane_resource.{kind}", {})
        body = resource.get("values", {})
        require(resource.get("mode") == "managed"
                and body.get("type") == "Microsoft.Foundry/agents@v1"
                and body.get("parent_id") == targets["project_endpoint"].removeprefix("https://")
                and body.get("name") == agent["name"]
                and body.get("body", {}).get("name") == agent["name"]
                and body.get("output", {}).get("agent_version") == agent["version"],
                "Missing or mismatched applied Terraform agent resource.")
        approved = body["body"].get("definition")
        definitions[kind] = managed_definition(approved, kind)
        require(approved == definitions[kind],
                "Applied definition contains unmanaged fields; extend the reviewed verifier before routing.")
    targets["definitions"] = definitions
    return targets, outputs


def desired_route(version):
    return {
        "version_selector": {"version_selection_rules": [
            {"agent_version": version, "traffic_percentage": 100, "type": "FixedRatio"}
        ]},
        "protocol_configuration": {"responses": {}},
    }


def normalize_route(agent):
    """Project only managed fields, ignoring URLs, metadata and other protocols."""
    endpoint = agent.get("agent_endpoint") or {}
    require(isinstance(endpoint, dict), "Malformed agent endpoint response.")
    selector = endpoint.get("version_selector") or {}
    protocols = endpoint.get("protocol_configuration") or {}
    require(isinstance(selector, dict) and isinstance(protocols, dict),
            "Malformed endpoint selector/protocol response.")
    rules = selector.get("version_selection_rules")
    if rules is not None:
        require(isinstance(rules, list), "Malformed endpoint selection rules.")
        managed = []
        for rule in rules:
            require(isinstance(rule, dict), "Malformed endpoint selection rule.")
            version, traffic, kind = (rule.get(key) for key in
                                     ("agent_version", "traffic_percentage", "type"))
            require((version is None or isinstance(version, str))
                    and (kind is None or isinstance(kind, str))
                    and (traffic is None or (type(traffic) in (int, float) and 0 <= traffic <= 100)),
                    "Malformed endpoint selection rule fields.")
            managed.append({"agent_version": version, "traffic_percentage": traffic, "type": kind})
        rules = sorted(managed, key=lambda item: json.dumps(item, sort_keys=True))
    responses = protocols.get("responses")
    require(responses is None or isinstance(responses, dict), "Malformed responses protocol configuration.")
    return {
        "version_selector": {"version_selection_rules": rules},
        "protocol_configuration": {"responses": {} if responses is not None else None},
    }


def agent_path(agent, version=False):
    path = "/agents/" + urllib.parse.quote(agent["name"], safe="")
    if version:
        path += "/versions/" + urllib.parse.quote(agent["version"], safe="")
    return path + "?api-version=v1"


def foundry_request(session, endpoint, path, *, patch=None, timeout=30):
    require(public_url(endpoint, ".services.ai.azure.com") == endpoint
            and re.fullmatch(r"/agents(?:/[a-zA-Z0-9_.-]+(?:/versions/[a-zA-Z0-9_.-]+)?)?"
                             r"\?api-version=v1", path),
            "Untrusted Foundry request target.")
    return azure_request(session, endpoint + path, "https://ai.azure.com", patch=patch, timeout=timeout)


def azure_request(session, url, audience, *, patch=None, timeout=30):
    require(audience in ("https://ai.azure.com", "https://search.azure.com",
                         "https://management.azure.com/"), "Unexpected token audience.")
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.port in (None, 443)
            and ((audience == "https://management.azure.com/" and parsed.hostname == "management.azure.com")
                 or (audience == "https://ai.azure.com"
                     and parsed.hostname.endswith(".services.ai.azure.com"))
                 or (audience == "https://search.azure.com"
                     and parsed.hostname.endswith(".search.windows.net"))),
            "Untrusted Azure request target/audience.")
    require(patch is None or audience == "https://ai.azure.com",
            "Only separately approved Foundry routing can mutate resources.")

    def budget():
        return min(30, timeout()) if callable(timeout) else timeout

    session.account(timeout=budget())
    token = command(executable("az") + [
        "account", "get-access-token", "--subscription", session.target["subscription_id"],
        "--resource", audience, "--query", "accessToken",
        "--output", "tsv", "--only-show-errors"], env=session.env, timeout=budget()).stdout.decode().strip()
    require(token and not any(character.isspace() for character in token),
            "Missing or malformed Azure token.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if patch is not None:
        headers["Content-Type"] = "application/merge-patch+json"
        body = json.dumps(patch, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers,
                                    method="PATCH" if patch is not None else "GET")
    # A new token and a non-redirecting HTTPS request for each operation; no CLI/token logs.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=budget()) as response:
        if patch is not None:
            return None  # Never persist a potentially sensitive PATCH response.
        require(response.status == 200, "Read-only Azure check did not return HTTP 200.")
        data = json.load(response)
    require(isinstance(data, dict), "Malformed Azure GET response.")
    return data


def permission_readiness(session, timeout=600):
    values = session.state()
    outputs = {key: item["value"] for key, item in values.get("outputs", {}).items()}
    require(type(outputs.get("deploy_workloads")) is bool and state_has_resources(values),
            "Apply the separately approved foundation before workload permission checks.")
    targets = project_target(outputs, session.target)
    resources = state_resources(values)
    for address, output in (("module.foundry.azapi_resource.project", "project_id"),
                            ("module.search.azapi_resource.service", "search_service_id")):
        require(resources.get(address, {}).get("values", {}).get("id") == outputs.get(output)
                and outputs.get(output),
                "Foundation resource is missing from applied state.")
    search_id = outputs.get("search_service_id", "")
    match = re.fullmatch(r"/subscriptions/" + re.escape(session.target["subscription_id"])
                        + r"/resourceGroups/[a-zA-Z0-9_.()-]+/providers/Microsoft\.Search"
                          r"/searchServices/([a-zA-Z0-9-]+)", search_id, re.I)
    require(match is not None, "Search ARM ID must belong to the requested subscription.")
    search = "https://" + match[1].lower() + ".search.windows.net"
    require(public_url(outputs.get("search_endpoint"), ".search.windows.net") == search,
            "Search endpoint does not match the applied service ARM ID.")
    names = outputs.get("workload_agent_names")
    if names is None:
        names = {kind: outputs.get(f"{kind}_agent_name") for kind in ("prompt", "hosted")}
    require(isinstance(names, dict) and set(names) == {"prompt", "hosted"}
            and all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]*", name)
                    for name in names.values()) and names["prompt"] != names["hosted"],
            "Foundation must output both configured agent names (workload_agent_names or direct name outputs).")
    index_name = outputs.get("search_index_name")
    require(isinstance(index_name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", index_name),
            "Foundation must output a valid Search index name.")
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        require(budget > 0, "Timed out waiting for workload data-plane permissions. "
                "Check applied operator RBAC and retry; no workload plan/apply was started.")
        return budget

    confirm_project(session, targets, timeout=remaining)
    # Collection 200 is required: an existence 404 alone is not authorization proof.
    checks = [
        ("Search index collection", lambda: azure_request(
            session, search + "/indexes?$select=name&api-version=2024-07-01",
            "https://search.azure.com", timeout=remaining), False, "value"),
        ("Search index", lambda: azure_request(
            session, search + "/indexes('" + index_name + "')?api-version=2024-07-01",
            "https://search.azure.com", timeout=remaining), True, None),
        ("Foundry agent collection", lambda: foundry_request(
            session, targets["project_endpoint"], "/agents?api-version=v1",
            timeout=remaining), False, "data"),
    ]
    for kind, name in names.items():
        checks.append((kind + " agent", lambda name=name: foundry_request(
            session, targets["project_endpoint"], agent_path({"name": name}),
            timeout=remaining), True, None))
    for label, check, allow_missing, collection in checks:
        while True:
            remaining()
            try:
                data = check()
                if collection:
                    require(isinstance(data.get(collection), list),
                            f"Malformed {label} response; cannot establish permission readiness.")
                remaining()
                break
            except urllib.error.HTTPError as error:
                if allow_missing and error.code == 404:
                    remaining()
                    break
                require(error.code != 401,
                        f"{label} returned HTTP 401 with a freshly requested audience-specific token. "
                        "Reauthenticate Azure CLI and check tenant/audience; no workload operation started.")
                if error.code != 403:
                    raise
                print(f"Waiting for applied RBAC propagation: {label} (HTTP 403).")
                time.sleep(min(10, remaining()))
    require(session.state() == values, "Applied foundation state changed during permission checks.")
    remaining()
    print("Read-only Search/Foundry permission checks passed; write permissions still depend on applied RBAC.")


def active_versions(session, targets, timeout=30):
    pending = []
    for kind, agent in targets["agents"].items():
        data = foundry_request(session, targets["project_endpoint"], agent_path(agent, version=True),
                              timeout=timeout)
        require(data.get("version") == agent["version"],
                "Exact version GET returned a different version.")
        require(managed_definition(data.get("definition"), kind) == targets["definitions"][kind],
                f"{kind.capitalize()} version definition differs from approved applied Terraform state; "
                "refusing routing/verification.")
        require(data.get("status") not in ("failed", "deleting", "deleted"),
                f"{kind.capitalize()} version failed or is deleting/deleted; inspect Foundry privately.")
        if data.get("status") != "active":
            pending.append(kind)
    return pending


def current_routes(session, targets, timeout=30):
    agents = {kind: foundry_request(session, targets["project_endpoint"], agent_path(agent), timeout=timeout)
              for kind, agent in targets["agents"].items()}
    require(all(agent.get("state") == "enabled" for agent in agents.values()),
            "Both agent objects must have state=enabled. No automatic enable/state change is permitted.")
    return {kind: normalize_route(agent) for kind, agent in agents.items()}, agents


def prepare_routes(target, targets, current):
    return {"target": dict(target), "outputs": targets, "current": current,
            "desired": {kind: desired_route(agent["version"])
                       for kind, agent in targets["agents"].items()}}


def show_routes(proposal):
    print("Target:", json.dumps(proposal["target"], sort_keys=True))
    print("Verified project ARM ID:", proposal["outputs"]["project_id"])
    print("Applied managed definitions SHA256:", source_hash(proposal["outputs"]["definitions"]))
    for kind, agent in proposal["outputs"]["agents"].items():
        print(f"{kind}:", json.dumps({
            "url": proposal["outputs"]["project_endpoint"] + agent_path(agent),
            "name": agent["name"], "version": agent["version"],
            "current": proposal["current"][kind], "desired": proposal["desired"][kind],
            "patch_required": proposal["current"][kind] != proposal["desired"][kind],
        }, sort_keys=True, allow_nan=False))


def route_agents(session):
    print("SEPARATELY APPROVED NON-PROVISIONER ROUTING: existing agents only; "
          "AzAPI 2.12 lacks data-plane PATCH support. Not Terraform or a third agent.")
    targets, _ = applied_targets(session)
    try:
        confirm_project(session, targets)
        require(not active_versions(session, targets),
                "Both prompt and hosted versions must be active. Wait, then retry route-agents; no PATCH sent.")
        current, _ = current_routes(session, targets)
    except urllib.error.HTTPError as error:
        raise DeploymentError(f"Route readiness/endpoint GET failed (HTTP {error.code}); no PATCH sent. "
                              "Check RBAC/readiness, then retry route-agents.") from None
    proposal = prepare_routes(session.target, targets, current)
    folder = private_dir(ARTIFACTS / "routes" / str(uuid.uuid4()))
    proposal["proposal_id"] = folder.name
    sha = source_hash(proposal)
    write_json(folder / "plan.json", proposal)
    write_private(folder / "plan.sha256", sha + "\n")
    show_routes(proposal)
    print("Route proposal SHA256:", sha)
    print("Private route proposal:", folder)
    changes = [kind for kind in targets["agents"] if current[kind] != proposal["desired"][kind]]
    result = {"proposal_sha256": sha, "status": "incomplete", "agents": {
        kind: {"status": "pending" if kind in changes else "already_matched",
               "observed": current[kind]} for kind in targets["agents"]
    }}
    write_json(folder / "result.json", result)
    if not changes:
        result["status"] = "no_changes"
        write_json(folder / "result.json", result)
        print("Both endpoints already select the Terraform versions with responses enabled; no PATCH needed.")
        return

    expected = dict(current)
    try:
        confirm(f"ROUTE {sha}")
        for kind in changes:
            require(source_hash(read_json(folder / "plan.json")) == sha
                    and source_hash(proposal) == sha,
                    "Route proposal changed; rerun route-agents for a new proposal.")
            session.account()
            require(session.target == proposal["target"]
                    and applied_targets(session)[0] == targets,
                    "Target or Terraform outputs changed; rerun route-agents for a new proposal.")
            confirm_project(session, targets)
            require(not active_versions(session, targets),
                    "Version readiness changed; wait and rerun route-agents for a new proposal.")
            observed, _ = current_routes(session, targets)
            require(observed == expected,
                    "Current endpoint routing changed; rerun route-agents for a new proposal.")
            # Recheck BOTH endpoints before each PATCH, including prior verified changes.
            result["agents"][kind]["status"] = "patch_attempted_unverified"
            write_json(folder / "result.json", result)
            try:
                foundry_request(session, targets["project_endpoint"], agent_path(targets["agents"][kind]),
                               patch={"agent_endpoint": proposal["desired"][kind]})
                agent = foundry_request(session, targets["project_endpoint"],
                                       agent_path(targets["agents"][kind]))
            except urllib.error.HTTPError as error:
                raise DeploymentError(f"Routing request failed (HTTP {error.code}); "
                                     "no automatic retry. Rerun route-agents to inspect remaining differences.") from None
            actual = normalize_route(agent)
            result["agents"][kind]["observed"] = actual
            require(agent.get("state") == "enabled" and actual == proposal["desired"][kind],
                    "PATCH not verified against expected selector/responses; rerun route-agents for a new proposal.")
            require(not active_versions(session, targets),
                    "Version definition/readiness changed after PATCH; routing is not verified.")
            expected[kind] = actual
            result["agents"][kind]["status"] = "verified"
            write_json(folder / "result.json", result)
        result["status"] = "complete"
        print("Approved endpoint routing verified. Next run verify.")
    finally:
        write_json(folder / "result.json", result)
        print("Private route result:", folder / "result.json")
        if result["status"] != "complete":
            print("Routing incomplete; attempted PATCHes may have succeeded. No rollback/retry was sent. "
                  "Rerun route-agents for a new proposal covering only remaining differences.")


def verify(session):
    targets, outputs = applied_targets(session)
    deadline = time.monotonic() + session.args.timeout

    def remaining():
        budget = deadline - time.monotonic()
        require(budget > 0, "Timed out waiting for both agent versions to be active; wait and retry verify.")
        return budget

    confirm_project(session, targets, timeout=remaining)
    while True:
        try:
            pending = active_versions(session, targets, timeout=remaining)
            remaining()
            if not pending:
                print("Both prompt and hosted agent versions are active.")
                current, agents = current_routes(session, targets, timeout=remaining)
                remaining()
                proposal = prepare_routes(session.target, targets, current)
                show_routes(proposal)
                require(current == proposal["desired"],
                       "Endpoint selection/protocol does not match Terraform. Run separately approved "
                       "route-agents, then retry verify; verify never changes routing.")
                require(applied_targets(session)[0] == targets,
                       "Terraform targets changed during verification; rerun route-agents, then verify.")
                identity = (agents["hosted"].get("instance_identity") or {}).get("principal_id")
                print("Hosted agent identity principal ID:",
                     json.dumps(identity) if identity else "not yet available (not project identity)")
                break
            print("Agent versions not active yet; waiting:", ", ".join(pending))
        except urllib.error.HTTPError as error:
            require(error.code in (404, 429, 500, 502, 503, 504),
                    f"Foundry readiness GET failed (HTTP {error.code}); check RBAC and endpoint.")
        time.sleep(min(10, remaining()))
    if outputs.get("ui_url"):
        health = public_url(outputs["ui_url"], ".azurecontainerapps.io") + "/health"
        session.account()
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(urllib.request.Request(health, method="GET"), timeout=30) as response:
            require(response.status == 200, "Public UI health endpoint did not return HTTP 200.")
        print("Public UI /health returned HTTP 200.")
    else:
        print("ui_url is not available; UI health was not verified.")


def parser():
    result = argparse.ArgumentParser(
        description="Staged deployment with separate, literal SHA256 approvals.",
        epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = result.add_subparsers(dest="command", required=True)
    for name, text in (
        ("init", "Initialize the configured backend; no plan, apply or migration."),
        ("plan-foundation", "Explicitly plan foundations only; never apply."),
        ("apply", "Show and separately approve one exact saved binary plan."),
        ("build-images", "Separately approve local linux/amd64 image build and ACR push."),
        ("plan-workloads", "Explicitly plan workloads with verified actual image digests."),
        ("route-agents", "Separately approved non-provisioner routing of existing agents only. "
         "Requires both versions active (otherwise wait/retry). Shows private SHA256-bound diffs; "
         "terminal ROUTE <sha256> approves PATCH, not Terraform apply. Then run verify."),
        ("verify", "Read-only readiness, actual endpoint selection for both agents, and UI health checks."),
    ):
        sub = commands.add_parser(name, help=text, description=text)
        sub.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
        sub.add_argument("--tfvars", required=True, help="Explicit environment tfvars path (relative to repo root).")
        if name == "init":
            backend = sub.add_mutually_exclusive_group(required=True)
            backend.add_argument("--local-development", action="store_true")
            backend.add_argument("--backend-config", help="Private existing azurerm backend config path.")
        if name == "apply":
            sub.add_argument("--stage", choices=("foundation", "workloads"), required=True)
            sub.add_argument("--plan", required=True, help="Saved deployment/.artifacts/plans/.../plan.bin.")
        if name in ("verify", "plan-workloads", "apply"):
            sub.add_argument("--timeout", type=int, default=600, help="Readiness deadline in seconds (1..3600).")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        require(1 <= getattr(args, "timeout", 600) <= 3600, "--timeout must be between 1 and 3600.")
        os.umask(0o077)
        private_dir(ARTIFACTS)
        session = Session(args)
        session.account()
        if args.command == "init":
            initialize(session)
        elif args.command == "plan-foundation":
            plan_stage(session, "foundation")
        elif args.command == "plan-workloads":
            plan_stage(session, "workloads")
        elif args.command == "apply":
            apply_stage(session)
        elif args.command == "build-images":
            build_images(session)
        elif args.command == "route-agents":
            route_agents(session)
        elif args.command == "verify":
            verify(session)
        return 0
    except DeploymentError as error:
        print(f"Refused: {error}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("Readiness CLI command timed out; no further operations performed.", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        print("Operation failed: missing/malformed input, local tool, or network response. "
              "Details withheld to protect credentials; inspect private artifacts.", file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; no further operations performed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
