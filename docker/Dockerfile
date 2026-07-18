# GPU docling image, adapted from docling's own official Dockerfile
# (https://github.com/docling-project/docling/blob/main/Dockerfile), swapping
# the CPU-forced torch wheel for a CUDA-enabled one. We don't need an
# nvidia/cuda base image: the torch wheels bundle their own CUDA runtime
# libs. The host only needs the NVIDIA driver + nvidia-container-toolkit,
# both preinstalled on the ECS_AL2_NVIDIA AMI used by the Batch compute
# environment, which the ECS agent wires into the container automatically
# based on the job definition's GPU resourceRequirements.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 curl wget procps \
    && rm -rf /var/lib/apt/lists/*

# Install torch/torchvision FIRST, pinned to an explicit cu124 build, via
# --index-url (not --extra-index-url). This is deliberate and load-bearing:
# plain `pip install docling` resolves torch from PyPI's default index,
# which as of torch 2.13 bundles CUDA 13 runtime deps by default - newer
# than what the ECS_AL2_NVIDIA AMI's NVIDIA driver supports (observed
# "CUDA initialization: The NVIDIA driver on your system is too old (found
# version 12040)" i.e. driver caps out at CUDA 12.4). --extra-index-url
# does NOT fix this: it only adds a candidate index, and pip still picks
# the highest overall version across both indices, which was the cu13
# build from PyPI. Pinning the exact version+build via --index-url up
# front, before docling is installed, forces this specific build and
# keeps pip's resolver from "upgrading" it back when docling's own
# torch<3.0.0,>=2.2.2 constraint is merely checked (not tightened) by the
# next install step. If AWS ships a newer driver on the ECS GPU AMI later,
# this pin can be bumped to a higher cuXXX build to match.
RUN pip install --no-cache-dir \
      "torch==2.6.0+cu124" "torchvision==0.21.0+cu124" \
      --index-url https://download.pytorch.org/whl/cu124

RUN pip install --no-cache-dir docling boto3

# Bake model weights into the image at build time (docling's own recommended
# pattern) so the running job never needs a runtime HuggingFace download.
ENV HF_HOME=/tmp/ TORCH_HOME=/tmp/ OMP_NUM_THREADS=4
RUN docling-tools models download

# `docling-tools models download` writes to settings.cache_dir / "models"
# (~/.cache/docling/models = /root/.cache/docling/models here, since this
# image runs as root and HF_HOME/TORCH_HOME above do NOT affect this path -
# cache_dir is a separate docling-specific setting). Setting
# DOCLING_ARTIFACTS_PATH is load-bearing, not cosmetic: without it, every
# model class (layout, tableformer, code/formula, RapidOCR) falls back to
# resolving its weights from its own remote source (HuggingFace Hub /
# ModelScope) on EVERY job run, regardless of what was baked in above -
# confirmed by seeing live huggingface.co/modelscope.cn HTTP requests in a
# real job's CloudWatch logs despite this RUN step having already downloaded
# everything at build time.
ENV DOCLING_ARTIFACTS_PATH=/root/.cache/docling/models

COPY entrypoint.py /usr/local/bin/entrypoint.py

ENTRYPOINT ["python3", "/usr/local/bin/entrypoint.py"]
