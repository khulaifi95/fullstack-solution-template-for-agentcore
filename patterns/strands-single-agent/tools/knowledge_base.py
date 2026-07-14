# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Knowledge Base retrieval tool for the Strands agent (RAG over the mock dataset)."""

import json
import os

import boto3
from strands import tool

_DEFAULT_TOP_K = 6


@tool
def search_knowledge_base(query: str) -> str:
    """Search the HCSA/HDB document knowledge base (policies, SOPs, emails, reports).

    Use this for any question that requires information from the source
    documents — workplace safety, sustainability, permits, procedures,
    annual/financial reports, or email correspondence. Returns the most
    relevant passages with their source document so you can cite them.

    Args:
        query: A natural-language question or search phrase.

    Returns:
        JSON string with a list of {text, source, score} passages.
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
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("type")
            or "unknown"
        )
        passages.append(
            {
                "text": r.get("content", {}).get("text", ""),
                "source": source,
                "score": r.get("score"),
            }
        )

    return json.dumps({"query": query, "results": passages}, default=str)
