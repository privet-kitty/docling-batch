#!/usr/bin/env python3
"""Upload a PDF, submit a docling GPU Batch job, poll to completion, download results.

Usage:
  scripts/submit_job.py my.pdf --output-dir ./out \\
      [--stack-name docling-batch] [--timeout 1800] [--poll-interval 15] \\
      [--enrich-code] [--enrich-formula] \\
      [--image-export-mode {placeholder,embedded,referenced}]

With --image-export-mode=referenced, expect two kinds of output locally:
  ./out/<name>.md
  ./out/<name>_artifacts/*.png
"""
import argparse
import sys
import time
import uuid
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from stack_outputs import get_stack_outputs  # noqa: E402

SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdf", type=Path, help="local PDF file to convert")
    parser.add_argument("--output-dir", type=Path, default=Path("./output"))
    parser.add_argument("--stack-name", default="docling-batch")
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="seconds to poll before giving up (the job itself keeps running)",
    )
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--enrich-code", action="store_true")
    parser.add_argument("--enrich-formula", action="store_true")
    parser.add_argument(
        "--image-export-mode",
        choices=["placeholder", "embedded", "referenced"],
        default="embedded",
    )
    return parser.parse_args()


def submit(batch, s3, outputs, args, job_uuid: str) -> str:
    bucket = outputs["BucketName"]
    input_key = f"input/{job_uuid}/{args.pdf.name}"
    output_prefix = f"output/{job_uuid}/"

    print(f"==> uploading {args.pdf} -> s3://{bucket}/{input_key}")
    s3.upload_file(str(args.pdf), bucket, input_key)

    env = [
        {"name": "INPUT_S3_URI", "value": f"s3://{bucket}/{input_key}"},
        {"name": "OUTPUT_S3_PREFIX", "value": f"s3://{bucket}/{output_prefix}"},
        {"name": "ENRICH_CODE", "value": str(args.enrich_code).lower()},
        {"name": "ENRICH_FORMULA", "value": str(args.enrich_formula).lower()},
        {"name": "IMAGE_EXPORT_MODE", "value": args.image_export_mode},
    ]

    job_name = f"docling-{job_uuid}"
    print(f"==> submitting job {job_name} to queue {outputs['JobQueueName']}")
    resp = batch.submit_job(
        jobName=job_name,
        jobQueue=outputs["JobQueueName"],
        jobDefinition=outputs["JobDefinitionName"],
        containerOverrides={"environment": env},
    )
    job_id = resp["jobId"]
    print(f"==> job id: {job_id}")
    return job_id, output_prefix


def poll(batch, job_id: str, timeout: int, poll_interval: int) -> dict:
    last_status = None
    deadline = time.monotonic() + timeout
    while True:
        desc = batch.describe_jobs(jobs=[job_id])["jobs"][0]
        status = desc["status"]
        if status != last_status:
            reason = desc.get("statusReason")
            print(f"==> status: {status}" + (f" ({reason})" if reason else ""))
            last_status = status
        if status in (SUCCEEDED, FAILED):
            return desc
        if time.monotonic() > deadline:
            print(
                f"==> timed out after {timeout}s waiting for job {job_id} (still {status}) - "
                f"the job keeps running; check `aws batch describe-jobs --jobs {job_id}`",
                file=sys.stderr,
            )
            sys.exit(1)
        time.sleep(poll_interval)


def report_failure(desc: dict, log_group: str) -> None:
    print(f"==> job FAILED: {desc.get('statusReason')}", file=sys.stderr)
    container = desc.get("container", {})
    reason = container.get("reason")
    exit_code = container.get("exitCode")
    log_stream = container.get("logStreamName")
    if reason:
        print(f"    container reason: {reason}", file=sys.stderr)
    if exit_code is not None:
        print(f"    container exit code: {exit_code}", file=sys.stderr)
    if log_stream:
        print(
            f"    logs: aws logs tail '{log_group}' --log-stream-names '{log_stream}'",
            file=sys.stderr,
        )


def download_output(s3, bucket: str, output_prefix: str, output_dir: Path) -> list:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=output_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(output_prefix):]
            if not rel:
                continue
            local_path = output_dir / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local_path))
            downloaded.append(local_path)
    return downloaded


def main() -> None:
    args = parse_args()
    if not args.pdf.is_file():
        print(f"error: no such file: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    outputs = get_stack_outputs(args.stack_name)
    bucket = outputs["BucketName"]

    s3 = boto3.client("s3")
    batch = boto3.client("batch")

    job_uuid = str(uuid.uuid4())
    job_id, output_prefix = submit(batch, s3, outputs, args, job_uuid)
    desc = poll(batch, job_id, args.timeout, args.poll_interval)

    if desc["status"] == FAILED:
        report_failure(desc, outputs.get("LogGroupName", "/docling-batch/jobs"))
        sys.exit(1)

    print(f"==> job SUCCEEDED, fetching s3://{bucket}/{output_prefix}")
    downloaded = download_output(s3, bucket, output_prefix, args.output_dir)

    if not downloaded:
        print("==> warning: job succeeded but no output files were found", file=sys.stderr)
        sys.exit(1)

    for path in sorted(downloaded):
        if path.suffix == ".md":
            print(f"==> markdown: {path}")
    artifacts = [p for p in downloaded if p.suffix != ".md"]
    if artifacts:
        print(f"==> plus {len(artifacts)} artifact file(s) under {args.output_dir}")


if __name__ == "__main__":
    main()
