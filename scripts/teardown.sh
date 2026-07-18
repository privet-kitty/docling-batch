#!/usr/bin/env bash
# Empty the S3 bucket (it doesn't auto-empty on stack deletion; the ECR repo
# does, via EmptyOnDelete: true in the template), then delete the stack.
set -euo pipefail

STACK_NAME="${STACK_NAME:-docling-batch}"

BUCKET="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" \
  --output text 2>/dev/null || true)"

if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  echo "==> emptying s3://$BUCKET"
  aws s3 rm "s3://$BUCKET" --recursive
else
  echo "==> could not resolve bucket name, skipping S3 empty step" >&2
fi

echo "==> deleting stack '$STACK_NAME'"
aws cloudformation delete-stack --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
echo "==> done"
