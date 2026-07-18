# docling-batch

Give it a local PDF; it spins up a GPU EC2 instance on demand via AWS Batch,
runs [docling](https://github.com/docling-project/docling) (GPU-accelerated
OCR + layout analysis) on it, and returns the converted Markdown (plus
extracted images, if requested) to a local directory.


## Architecture

- CloudFormation (`cloudformation/template.yaml`): a VPC (public subnets +
  Internet Gateway, no NAT Gateway), an ECR repository, an S3 bucket for
  input/output transfer, IAM roles, a CloudWatch Logs group, and an AWS Batch
  compute environment / job queue / job definition.
- AWS Batch (EC2): a managed compute environment using `g4dn.xlarge`
  (NVIDIA T4) instances, `MinvCpus: 0`, and the `ECS_AL2_NVIDIA`
  GPU-optimized AMI.
- Docker image (`docker/`): docling + a CUDA-enabled torch build, with
  model weights baked in at build time (`docling-tools models download`).
- OCR engine: currently forced to RapidOCR with the `torch` backend. `lang` is
  also forced to `["english"]` in `entrypoint.py`.
- Local scripts (`scripts/`): build/deploy/submit/teardown.

## Setup

Prerequisites: AWS CLI configured, Docker installed, an AWS account
with EC2/Batch/ECR/S3/IAM/CloudFormation permissions.

> [!NOTE]
> Request at least 4 vCPUs for "Running On-Demand G and VT instances" before your first job: Service Quotas console → EC2 → "Running On-Demand G and VT instances".

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
  job itself is not cancelled; the script just stops watching it. After timeout,
  you will need to manually retrieve the results from S3.

On failure, the script prints the job's status reason, container exit code,
and a ready-to-run `aws logs tail` command against the job's CloudWatch log
stream.

```
scripts/teardown.sh   # empties the S3 bucket, then deletes the stack
```

## Operational notes

- **torch/CUDA version is pinned** in the Dockerfile
  (`torch==2.6.0+cu124`/`torchvision==0.21.0+cu124`). If jobs fail with
  `exit code 6` after an AWS AMI update, check the CloudWatch log for the
  driver's reported CUDA version and bump this pin to match.
- **Job timeout is tunable**: `--enrich-code`/`--enrich-formula` can push job
  duration well past plain OCR. If a job hits the Batch timeout
  (`exit code 137`), raise it via
  `JOB_TIMEOUT_SECONDS=<seconds> scripts/deploy.sh` (default 14400s/4h).
- **`DOCLING_ARTIFACTS_PATH`** must stay set (already set in the Dockerfile)
  for models to load from the baked-in cache instead of fetching remotely on
  every job run.
- **Cold start**: `MinvCpus: 0` means the first job after idle time pays for
  a fresh instance boot + image pull — expect several minutes end-to-end.
  Back-to-back jobs reuse the warm instance and are much faster.
- **`:latest` tag**: the job definition points at the mutable `:latest` ECR
  tag, so `build_and_push.sh` updates all future jobs immediately without a
  template redeploy.
- **S3 lifecycle**: `input/` and `output/` objects expire after 30 days.
- Compute instances get a public IP for outbound egress only (no NAT); the
  security group has no inbound rules.
