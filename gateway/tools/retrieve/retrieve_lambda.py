# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AgentCore Gateway tool: retrieve from the HDB Bedrock Knowledge Base.

Unstructured retrieval lane (see docs/HDB_KM_CHATBOT_ARCHITECTURE.md). Queries
the Bedrock KB and returns chunks with their source document, space, and page
number so the agent can produce page-level citations.

Access control: an optional `spaces` argument restricts retrieval to specific
spaces via a KB metadata filter. The orchestrator is expected to derive the
allowed spaces from the caller's Cognito groups and pass them in, so a user
never retrieves from a space they aren't entitled to.

Config: the KB id is read from SSM at cold start (parameter name in env
KB_ID_PARAM), falling back to the KNOWLEDGE_BASE_ID env var.

One tool per Lambda (FAST convention). Returns {"content": [{"type":"text",...}]}.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_agent_rt = boto3.client("bedrock-agent-runtime")
_ssm = boto3.client("ssm")

# Resolve the KB id once per container.
_KB_ID = os.environ.get("KNOWLEDGE_BASE_ID")
if not _KB_ID:
    param = os.environ.get("KB_ID_PARAM")
    if param:
        try:
            _KB_ID = _ssm.get_parameter(Name=param)["Parameter"]["Value"]
        except Exception:  # pragma: no cover - surfaced at invoke time instead
            logger.exception("Failed to read KB id from SSM param %s", param)


def _build_filter(spaces):
    """Build a KB metadata filter that limits results to the allowed spaces."""
    if not spaces:
        return None
    if len(spaces) == 1:
        return {"equals": {"key": "space", "value": spaces[0]}}
    return {"orAll": [{"equals": {"key": "space", "value": s}} for s in spaces]}


def retrieve(query: str, spaces=None, max_results: int = 5) -> dict:
    """Retrieve relevant chunks from the KB with citation metadata."""
    if not _KB_ID:
        return {"error": "Knowledge base id not configured (set KNOWLEDGE_BASE_ID or KB_ID_PARAM)."}

    vector_config = {"numberOfResults": max_results}
    meta_filter = _build_filter(spaces)
    if meta_filter:
        vector_config["filter"] = meta_filter

    resp = _agent_rt.retrieve(
        knowledgeBaseId=_KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_config},
    )

    results = []
    for r in resp.get("retrievalResults", []):
        meta = r.get("metadata", {}) or {}
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
        source = uri.rsplit("/", 1)[-1] if uri else meta.get("source_file", "")
        page = meta.get("x-amz-bedrock-kb-document-page-number")
        results.append(
            {
                "text": r.get("content", {}).get("text", ""),
                "score": r.get("score"),
                "source": source,
                "space": meta.get("space"),
                "page": int(page) if isinstance(page, (int, float)) else page,
            }
        )
    return {"results": results}


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event))
    try:
        delimiter = "___"
        full = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = full[full.index(delimiter) + len(delimiter):]
        logger.info("Processing tool: %s", tool_name)

        if tool_name != "retrieve":
            logger.error("Unexpected tool name: %s", tool_name)
            return {"error": f"This Lambda only supports 'retrieve', received: {tool_name}"}

        query = event.get("query", "")
        if not query:
            return {"error": "Missing required argument: query"}
        spaces = event.get("spaces")
        max_results = int(event.get("max_results", 5))

        out = retrieve(query, spaces=spaces, max_results=max_results)
        return {"content": [{"type": "text", "text": json.dumps(out)}]}
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing request")
        return {"error": f"Internal server error: {str(e)}"}
