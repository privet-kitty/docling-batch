# docling-batch

Give it a local PDF; it spins up a GPU EC2 instance on demand via AWS Batch,
runs [docling](https://github.com/docling-project/docling) (GPU-accelerated
OCR + layout analysis) on it, and returns the converted Markdown (plus
extracted images, if requested) to a local directory. Scales to zero cost
when idle — no GPU instance runs unless a job is active.

Docling does **not** produce a searchable PDF with a text layer (that's a
different tool, e.g. `ocrmypdf`) — it produces a structured document exported
here as Markdown.

## Architecture

- **CloudFormation** (`cloudformation/template.yaml`): a dedicated minimal VPC
  (public subnets + Internet Gateway, no NAT Gateway — GPU instances already
  dominate cost, and NAT would bill 24/7 for a system designed to scale to
  zero), an ECR repository, an S3 bucket for input/output transfer, IAM roles,
  a CloudWatch Logs group, and an AWS Batch compute environment / job queue /
  job definition.
- **AWS Batch** (EC2, not Fargate — Fargate has no GPU support): a managed
  compute environment using `g4dn.xlarge` (NVIDIA T4) instances, `MinvCpus: 0`
  so it costs nothing while idle, and the `ECS_AL2_NVIDIA` GPU-optimized AMI
  (drivers + nvidia-container-toolkit preinstalled, no custom AMI needed).
- **Docker image** (`docker/`): docling + a CUDA-enabled torch build, with
  model weights baked in at build time (`docling-tools models download`) so
  jobs never need a runtime model download.
- **OCR engine**: forced to **RapidOCR with the `torch` backend**, not
  docling's "auto" default. Per docling's own [GPU support
  guide](https://docling-project.github.io/docling/usage/gpu/), RapidOCR+torch
  is the only OCR engine currently confirmed to use the GPU — other engines'
  GPU support depends on third-party library internals and isn't guaranteed.
  `lang` is also forced to `["english"]` in `entrypoint.py`, since
  `RapidOcrOptions` otherwise defaults to `lang=["chinese"]`, which silently
  drops inter-word spacing when applied to Latin-script text.
- **Local scripts** (`scripts/`): build/deploy/submit/teardown.

## Setup

Prerequisites: `aws configure` already run, Docker installed, an AWS account
with EC2/Batch/ECR/S3/IAM/CloudFormation permissions.

**EC2 GPU service quota** — most AWS accounts default the "Running On-Demand
G and VT instances" vCPU quota to a small or zero value. If it's too low,
jobs will sit in `RUNNABLE` forever with no clear error. Check/request it
before your first real job: Service Quotas console → EC2 → "Running
On-Demand G and VT instances" → request at least 4 vCPUs.

```
pip install -r requirements-dev.txt   # boto3 + cfn-lint, for the local scripts

scripts/deploy.sh          # one-time, or after template changes
scripts/build_and_push.sh  # one-time, or after Dockerfile/docling changes
```

## Usage

```
scripts/submit_job.py my.pdf --output-dir ./out
# -> ./out/my.md   (embedded images, default)
```

With docling's enrichment and image-export options (flag names mirror
docling's own CLI):

```
scripts/submit_job.py my.pdf --output-dir ./out \
  --enrich-code --enrich-formula --image-export-mode=referenced
# -> ./out/my.md
# -> ./out/my_artifacts/*.png
```

- `--enrich-code` — enable the code-block enrichment model.
- `--enrich-formula` — enable the formula (LaTeX) enrichment model.
- `--image-export-mode` — `placeholder` (no images), `embedded` (base64
  inline in the Markdown, default), or `referenced` (images saved as
  separate PNGs in a sibling `<name>_artifacts/` directory, linked by
  relative path from the Markdown).
- `--stack-name` — defaults to `docling-batch`; override to match `deploy.sh`
  if you deployed under a different name.
- `--timeout` — seconds to poll before giving up (default 1800s). The Batch
  job itself is not cancelled; the script just stops watching it.

On failure, the script prints the job's status reason, container exit code,
and a ready-to-run `aws logs tail` command against the job's CloudWatch log
stream.

```
scripts/teardown.sh   # empties the S3 bucket, then deletes the stack
```

## Operational notes

- **torch/CUDA version is pinned, deliberately**: the Dockerfile installs
  `torch==2.6.0+cu124`/`torchvision==0.21.0+cu124` explicitly *before*
  installing docling. Plain `pip install docling` resolves torch from
  PyPI's default index, which now bundles CUDA 13 runtime deps by default —
  newer than what the ECS_AL2_NVIDIA AMI's NVIDIA driver supports as of
  this writing (observed failure: `CUDA initialization: The NVIDIA driver
  on your system is too old (found version 12040)`, i.e. driver caps out
  at CUDA 12.4). `--extra-index-url` does *not* fix this — pip still picks
  the highest version across all indices, which was the CUDA-13 build from
  PyPI. If jobs start failing again with `exit code 6` (the GPU self-check
  in `entrypoint.py`) after an AWS AMI update, check the CloudWatch log for
  the driver's reported CUDA version and bump this pin to match.
- **Cold start**: `MinvCpus: 0` means the first job after idle time pays for
  a fresh instance boot + a multi-GB ECR image pull on top of actual
  conversion time — expect several minutes end-to-end. Back-to-back jobs
  reuse the still-warm instance and cached image layers and are much
  faster. This latency-vs-idle-cost trade-off is deliberate.
- **Image size**: the final image is multi-GB (CUDA torch wheels + baked
  docling model weights), which drives the cold-pull time above and ongoing
  ECR storage cost.
- **`:latest` tag**: the job definition always points at the mutable
  `:latest` ECR tag, so `build_and_push.sh` updates all future jobs
  immediately without a template redeploy. Simple, but not
  reproducible/rollback-able — a future iteration could tag by content hash
  and register new job-definition revisions instead.
- **S3 lifecycle**: `input/` and `output/` objects expire after 30 days —
  this bucket is a transient transfer point, not an archive.
- **Public IPs, no inbound**: compute instances get a public IP purely for
  outbound egress via the Internet Gateway (there's no NAT); the actual
  protection is the security group having zero inbound rules, not the
  absence of a public IP.
