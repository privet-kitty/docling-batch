#!/usr/bin/env bash
# Build the GPU docling image and push it to the ECR repo created by
# deploy.sh. The job definition always points at the mutable ":latest" tag,
# so re-running this script updates all future job runs without a template
# redeploy - simple, but not reproducible/rollback-able. A future iteration
# could tag by content hash and register new job-definition revisions.
set -euo pipefail

STACK_NAME="${STACK_NAME:-docling-batch}"
AWS_REGION="${AWS_REGION:-$(aws configure get region)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/../docker"

REPO_URI="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" \
  --output text)"

if [[ -z "$REPO_URI" || "$REPO_URI" == "None" ]]; then
  echo "error: could not resolve RepositoryUri from stack '$STACK_NAME' - did you run deploy.sh?" >&2
  exit 1
fi

REGISTRY="${REPO_URI%%/*}"

echo "==> logging in to $REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> building $REPO_URI:latest"
docker build -t "$REPO_URI:latest" -f "$DOCKER_DIR/Dockerfile" "$DOCKER_DIR"

echo "==> pushing $REPO_URI:latest"
docker push "$REPO_URI:latest"
