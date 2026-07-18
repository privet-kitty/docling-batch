#!/usr/bin/env bash
# Lint, validate, and deploy the CloudFormation stack. Safe to re-run after
# template changes (aws cloudformation deploy is idempotent / no-ops if
# nothing changed).
set -euo pipefail

STACK_NAME="${STACK_NAME:-docling-batch}"
MAX_VCPUS="${MAX_VCPUS:-16}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-14400}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../cloudformation/template.yaml"

if command -v cfn-lint >/dev/null 2>&1; then
  echo "==> cfn-lint"
  cfn-lint "$TEMPLATE"
else
  echo "==> cfn-lint not installed, skipping (pip install -r requirements-dev.txt to enable)" >&2
fi

echo "==> validate-template"
aws cloudformation validate-template --template-body "file://$TEMPLATE" >/dev/null

echo "==> deploying stack '$STACK_NAME'"
aws cloudformation deploy \
  --template-file "$TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ProjectName="$STACK_NAME" MaxvCpus="$MAX_VCPUS" JobTimeoutSeconds="$JOB_TIMEOUT_SECONDS"

echo "==> stack outputs"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs" \
  --output table
