# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Knowledge Base retrieval tool for the Strands agent (RAG over the mock dataset)."""

import json
import os
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from strands import tool

_DEFAULT_TOP_K = 6
# Presigned link lifetime (seconds). Long enough to click from a chat response.
_LINK_TTL = 3600


def _presign(s3_uri: str) -> str:
    """Turn an s3://bucket/key URI into a temporary clickable HTTPS URL.

    Returns an empty string if the URI isn't an S3 location or presigning fails,
    so a missing link never breaks the response.
    """
    if not s3_uri or not s3_uri.startswith("s3://"):
        return ""
    parsed = urlparse(s3_uri)
    bucket, key = parsed.netloc, parsed.path.lstrip("/")
    if not bucket or not key:
        return ""
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    try:
        # Force SigV4 + the regional endpoint so the presigned URL validates
        # (the default global endpoint yields 403s for regional buckets).
        client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_LINK_TTL,
        )
    except (ClientError, Exception):  # noqa: BLE001 - link is best-effort
        return ""


@tool
def search_knowledge_base(query: str) -> str:
    """Search the HCSA/HDB document knowledge base (policies, SOPs, emails, reports).

    Use this for any question that requires information from the source
    documents — workplace safety, sustainability, permits, procedures,
    annual/financial reports, or email correspondence. Returns the most
    relevant passages, each with its source document name and a clickable
    reference link (a temporary URL to the source PDF).

    When you use information from a result, cite it in your answer and include
    its `link` as a markdown link, e.g. "([SOP-CO-003.pdf](<link>))". End your
    response with a "References" section listing each cited document as a
    markdown link.

    Args:
        query: A natural-language question or search phrase.

    Returns:
        JSON string with a list of {text, source, link, score} passages.
    """
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
    if not kb_id:
        return json.dumps({"error": "Knowledge base is not configured for this deployment."})

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    top_k = int(os.environ.get("KB_TOP_K", str(_DEFAULT_TOP_K)))
    client = boto3.client("bedrock-agent-runtime", region_name=region)

    try:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": top_k}
            },
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Knowledge base retrieval failed: {exc}"})

    passages = []
    for r in resp.get("retrievalResults", []):
        location = r.get("location", {})
        s3_uri = location.get("s3Location", {}).get("uri", "")
        # Human-friendly document name (the file, without the bucket path).
        source = s3_uri.rsplit("/", 1)[-1] if s3_uri else location.get("type", "unknown")
        passages.append(
            {
                "text": r.get("content", {}).get("text", ""),
                "source": source,
                "link": _presign(s3_uri),
                "score": r.get("score"),
            }
        )

    return json.dumps({"query": query, "results": passages}, default=str)
