# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Custom-resource Lambda that starts a Bedrock Knowledge Base ingestion job at
deploy time and polls it to completion.

On Create/Update it calls StartIngestionJob for the (knowledgeBaseId,
dataSourceId) and waits until the job leaves IN_PROGRESS. On Delete it is a
no-op (the KB and its vectors are torn down by CloudFormation). Ingestion
failures are logged but do NOT fail the deployment — a partial/empty KB is
preferable to a rolled-back stack for a POC.
"""

import time

import boto3

bedrock_agent = boto3.client("bedrock-agent")

TERMINAL = {"COMPLETE", "FAILED"}
POLL_SECONDS = 15
MAX_WAIT_SECONDS = 13 * 60  # leave headroom under the 15-min Lambda timeout


def handler(event, context):
    """Entry point for the CDK custom-resource provider."""
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    kb_id = props.get("KnowledgeBaseId")
    ds_id = props.get("DataSourceId")

    print(f"RequestType={request_type} kb={kb_id} ds={ds_id}")

    if request_type == "Delete":
        return {"PhysicalResourceId": f"kb-ingest-{kb_id}-{ds_id}"}

    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            description="Deploy-time ingestion of mock dataset PDFs",
        )
        job_id = resp["ingestionJob"]["ingestionJobId"]
        print(f"Started ingestion job {job_id}")
        _wait_for_job(kb_id, ds_id, job_id)
    except Exception as exc:  # noqa: BLE001 - never fail the deploy on ingestion
        print(f"WARNING: ingestion could not complete: {exc}")

    return {"PhysicalResourceId": f"kb-ingest-{kb_id}-{ds_id}"}


def _wait_for_job(kb_id, ds_id, job_id):
    """Poll the ingestion job until it reaches a terminal state or times out."""
    waited = 0
    while waited < MAX_WAIT_SECONDS:
        job = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id,
        )["ingestionJob"]
        status = job["status"]
        stats = job.get("statistics", {})
        print(f"Ingestion job {job_id} status={status} stats={stats}")
        if status in TERMINAL:
            if status == "FAILED":
                print(f"WARNING: ingestion job failed: {job.get('failureReasons')}")
            return
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    print(f"WARNING: ingestion job {job_id} still running after {waited}s; not waiting further")
