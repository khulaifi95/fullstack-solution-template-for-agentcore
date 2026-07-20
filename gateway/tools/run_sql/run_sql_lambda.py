# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AgentCore Gateway tool: run read-only SQL over the HDB structured datasets.

Structured retrieval lane (see docs/HDB_KM_CHATBOT_ARCHITECTURE.md). Executes a
Presto/Trino SELECT against the Glue-catalogued Parquet tables via Amazon Athena
and returns rows as JSON. Powers relational questions (joins, aggregates) that
vector search cannot answer — e.g. "which contractors' projects failed inspection".

Safety: only single read-only SELECT/WITH statements are allowed. Any DDL/DML
(INSERT/UPDATE/DELETE/DROP/CREATE/ALTER, etc.) or multiple statements are rejected
before execution.

Config (env, with SSM fallbacks): ATHENA_DATABASE / DB_PARAM,
ATHENA_WORKGROUP / WORKGROUP_PARAM.

One tool per Lambda (FAST convention). Returns {"content": [{"type":"text",...}]}.
"""

import json
import logging
import os
import re
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_athena = boto3.client("athena")
_ssm = boto3.client("ssm")

_MAX_ROWS = 200
_POLL_SECONDS = 25


def _resolve(env_name: str, param_env: str) -> str:
    val = os.environ.get(env_name)
    if val:
        return val
    param = os.environ.get(param_env)
    if param:
        try:
            return _ssm.get_parameter(Name=param)["Parameter"]["Value"]
        except Exception:  # pragma: no cover
            logger.exception("Failed to read %s from SSM", param)
    return ""


_DATABASE = _resolve("ATHENA_DATABASE", "DB_PARAM") or "hdb_structured"
_WORKGROUP = _resolve("ATHENA_WORKGROUP", "WORKGROUP_PARAM")

# Reject anything that isn't a single read-only query.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|grant|revoke|merge|"
    r"replace|msck|call)\b",
    re.IGNORECASE,
)


def _is_read_only(sql: str) -> bool:
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:  # no multi-statement
        return False
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        return False
    return not _FORBIDDEN.search(stripped)


def run_sql(sql: str) -> dict:
    if not _WORKGROUP:
        return {"error": "Athena workgroup not configured (set ATHENA_WORKGROUP or WORKGROUP_PARAM)."}
    if not _is_read_only(sql):
        return {"error": "Only a single read-only SELECT/WITH statement is allowed."}

    start = _athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": _DATABASE},
        WorkGroup=_WORKGROUP,
    )
    qid = start["QueryExecutionId"]

    deadline = time.time() + _POLL_SECONDS
    state = "RUNNING"
    while time.time() < deadline:
        info = _athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)

    if state != "SUCCEEDED":
        reason = info.get("Status", {}).get("StateChangeReason", state)
        return {"error": f"Query {state}: {reason}", "query_execution_id": qid}

    res = _athena.get_query_results(QueryExecutionId=qid, MaxResults=_MAX_ROWS + 1)
    rows = res["ResultSet"]["Rows"]
    if not rows:
        return {"columns": [], "rows": []}
    columns = [c.get("VarCharValue", "") for c in rows[0]["Data"]]
    data = [
        {columns[i]: cell.get("VarCharValue") for i, cell in enumerate(r["Data"])}
        for r in rows[1:]
    ]
    return {"columns": columns, "rows": data, "row_count": len(data), "query_execution_id": qid}


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event))
    try:
        delimiter = "___"
        full = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = full[full.index(delimiter) + len(delimiter):]
        logger.info("Processing tool: %s", tool_name)

        if tool_name != "run_sql":
            logger.error("Unexpected tool name: %s", tool_name)
            return {"error": f"This Lambda only supports 'run_sql', received: {tool_name}"}

        sql = event.get("sql", "")
        if not sql:
            return {"error": "Missing required argument: sql"}

        out = run_sql(sql)
        return {"content": [{"type": "text", "text": json.dumps(out)}]}
    except Exception as e:  # noqa: BLE001
        logger.exception("Error processing request")
        return {"error": f"Internal server error: {str(e)}"}
