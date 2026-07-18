"""Shared helper: read a CloudFormation stack's Outputs as a plain dict."""
from typing import Dict, Optional

import boto3


def get_stack_outputs(stack_name: str, region: Optional[str] = None) -> Dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stacks(StackName=stack_name)
    stacks = resp.get("Stacks", [])
    if not stacks:
        raise RuntimeError(f"stack not found: {stack_name}")
    outputs = stacks[0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}
