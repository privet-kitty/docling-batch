#!/usr/bin/env python3
"""AWS Batch job entrypoint: S3 download -> docling GPU conversion -> S3 upload.

Env in (set per-job by scripts/submit_job.py via containerOverrides.environment):
  INPUT_S3_URI     s3://bucket/key of the source PDF
  OUTPUT_S3_PREFIX s3://bucket/prefix/ (trailing slash) to upload results under.
                   A prefix, not a single key, because IMAGE_EXPORT_MODE=referenced
                   produces multiple files: <stem>.md and <stem>_artifacts/*.png
  ENRICH_CODE      "true"/"false", default "false"
  ENRICH_FORMULA   "true"/"false", default "false"
  IMAGE_EXPORT_MODE "placeholder" | "embedded" | "referenced", default "embedded"

Exit codes: 2 bad env vars, 3 S3 download failure, 4 docling conversion/export
failure, 5 S3 upload failure, 6 GPU not available, 0 success.
"""
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("docling-entrypoint")

VALID_IMAGE_EXPORT_MODES = ("placeholder", "embedded", "referenced")


def parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"not a valid s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def load_config() -> dict:
    input_s3_uri = os.environ.get("INPUT_S3_URI", "").strip()
    output_s3_prefix = os.environ.get("OUTPUT_S3_PREFIX", "").strip()
    image_export_mode = os.environ.get("IMAGE_EXPORT_MODE", "embedded").strip().lower()

    errors = []
    if not input_s3_uri:
        errors.append("INPUT_S3_URI is required")
    if not output_s3_prefix:
        errors.append("OUTPUT_S3_PREFIX is required")
    elif not output_s3_prefix.endswith("/"):
        output_s3_prefix += "/"
    if image_export_mode not in VALID_IMAGE_EXPORT_MODES:
        errors.append(
            f"IMAGE_EXPORT_MODE={image_export_mode!r} must be one of {VALID_IMAGE_EXPORT_MODES}"
        )

    try:
        input_bucket, input_key = parse_s3_uri(input_s3_uri) if input_s3_uri else (None, None)
    except ValueError as exc:
        errors.append(str(exc))
        input_bucket = input_key = None

    try:
        output_bucket, output_prefix = parse_s3_uri(output_s3_prefix) if output_s3_prefix else (None, None)
    except ValueError as exc:
        errors.append(str(exc))
        output_bucket = output_prefix = None

    if errors:
        for err in errors:
            log.error("config error: %s", err)
        sys.exit(2)

    return {
        "input_bucket": input_bucket,
        "input_key": input_key,
        "output_bucket": output_bucket,
        "output_prefix": output_prefix,
        "enrich_code": parse_bool(os.environ.get("ENRICH_CODE", "false")),
        "enrich_formula": parse_bool(os.environ.get("ENRICH_FORMULA", "false")),
        "image_export_mode": image_export_mode,
    }


def check_gpu() -> None:
    import torch

    if not torch.cuda.is_available():
        log.error("torch.cuda.is_available() is False - no GPU visible to this container")
        sys.exit(6)
    log.info("GPU OK: %s", torch.cuda.get_device_name(0))


def download_input(s3, bucket: str, key: str, work_dir: Path) -> Path:
    local_path = work_dir / Path(key).name
    log.info("downloading s3://%s/%s -> %s", bucket, key, local_path)
    start = time.monotonic()
    try:
        s3.download_file(bucket, key, str(local_path))
    except ClientError:
        log.exception("failed to download input from S3")
        sys.exit(3)
    log.info("download done in %.1fs", time.monotonic() - start)
    return local_path


def convert(local_pdf: Path, out_dir: Path, cfg: dict) -> Path:
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    # Per https://docling-project.github.io/docling/usage/gpu/, RapidOCR with
    # the torch backend is the only OCR engine known to actually use the GPU
    # - the "auto" OCR engine default is not guaranteed to pick it. lang is
    # forced to English because RapidOcrOptions defaults to lang=["chinese"],
    # which silently drops inter-word spacing on Latin-script text (verified
    # against the installed docling/docling-core source).
    pipeline_options.ocr_options = RapidOcrOptions(backend="torch", lang=["english"])
    # Force CUDA explicitly rather than AcceleratorDevice.AUTO: on a GPU box
    # we want a loud failure (see check_gpu) instead of a silent, slow
    # fallback to CPU if something's misconfigured. This is read by both the
    # layout/table models and, via RapidOcrModel's decide_device() call, by
    # RapidOCR itself.
    pipeline_options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CUDA)
    pipeline_options.do_code_enrichment = cfg["enrich_code"]
    pipeline_options.do_formula_enrichment = cfg["enrich_formula"]
    if cfg["image_export_mode"] == "referenced":
        # Required or images silently degrade to placeholders even in
        # referenced mode - a documented docling gotcha. Do not remove.
        pipeline_options.generate_picture_images = True

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )

    log.info(
        "converting %s (enrich_code=%s enrich_formula=%s image_export_mode=%s)",
        local_pdf,
        cfg["enrich_code"],
        cfg["enrich_formula"],
        cfg["image_export_mode"],
    )
    start = time.monotonic()
    try:
        result = converter.convert(str(local_pdf))
        stem = local_pdf.stem
        md_path = out_dir / f"{stem}.md"
        # save_as_markdown, not export_to_markdown: export_to_markdown has a
        # known bug where image_mode=REFERENCED doesn't materialize the
        # <stem>_artifacts/ image files, only save_as_markdown does.
        #
        # save_as_markdown derives the image link path from the *filename*
        # argument's own parent directory, not from cwd independently:
        # passing the absolute out_dir path here would bake this container's
        # temp path (e.g. /tmp/xyz/output/report_artifacts/img.png) straight
        # into the markdown, which is meaningless once downloaded locally.
        # Chdir into out_dir and pass a bare relative filename so the link
        # comes out as a clean "<stem>_artifacts/..." relative reference
        # (verified against the installed docling_core package).
        cwd_before = os.getcwd()
        os.chdir(out_dir)
        try:
            result.document.save_as_markdown(
                Path(f"{stem}.md"), image_mode=ImageRefMode(cfg["image_export_mode"])
            )
        finally:
            os.chdir(cwd_before)
    except Exception:
        log.exception("docling conversion/export failed")
        sys.exit(4)
    log.info("conversion done in %.1fs -> %s", time.monotonic() - start, md_path)
    return md_path


def upload_output(s3, out_dir: Path, bucket: str, prefix: str) -> None:
    files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    log.info("uploading %d file(s) to s3://%s/%s", len(files), bucket, prefix)
    start = time.monotonic()
    try:
        for path in files:
            rel = path.relative_to(out_dir).as_posix()
            key = f"{prefix}{rel}"
            s3.upload_file(str(path), bucket, key)
            log.info("  uploaded s3://%s/%s", bucket, key)
    except ClientError:
        log.exception("failed to upload output to S3")
        sys.exit(5)
    log.info("upload done in %.1fs", time.monotonic() - start)


def main() -> None:
    cfg = load_config()
    check_gpu()

    s3 = boto3.client("s3")
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        out_dir = work_dir / "output"
        out_dir.mkdir()

        local_pdf = download_input(s3, cfg["input_bucket"], cfg["input_key"], work_dir)
        convert(local_pdf, out_dir, cfg)
        upload_output(s3, out_dir, cfg["output_bucket"], cfg["output_prefix"])

    log.info("done")
    sys.exit(0)


if __name__ == "__main__":
    main()
