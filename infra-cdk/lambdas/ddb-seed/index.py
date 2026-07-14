# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Custom-resource Lambda that seeds the structured-data DynamoDB table from the
JSON files deployed under s3://<bucket>/<prefix> at deploy time.

Each JSON file is an array of record objects (one file per dataset:
contractors, projects, permits, inspections). The file's slug becomes the
entity type. Each record is written with:
    pk = "<ENTITY>#<id>"   (id = first key containing "id", else row index)
    sk = "<ENTITY>"
plus an "entity" attribute and all original columns. This lets the agent's
query tool look up by Project ID / Contractor ID across datasets.

On Delete it is a no-op (the table is removed by CloudFormation). Seeding
errors are logged but never fail the deployment.
"""

import decimal
import json
import re

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    """Entry point for the CDK custom-resource provider."""
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    bucket = props.get("Bucket")
    prefix = props.get("Prefix", "structured/")
    table_name = props.get("TableName")

    print(f"RequestType={request_type} bucket={bucket} prefix={prefix} table={table_name}")

    if request_type == "Delete":
        return {"PhysicalResourceId": f"ddb-seed-{table_name}"}

    try:
        _seed(bucket, prefix, table_name)
    except Exception as exc:  # noqa: BLE001 - never fail the deploy on seeding
        print(f"WARNING: seeding could not complete: {exc}")

    return {"PhysicalResourceId": f"ddb-seed-{table_name}"}


def _seed(bucket, prefix, table_name):
    """Load every JSON file under the prefix and batch-write records."""
    table = dynamodb.Table(table_name)
    keys = _list_json_keys(bucket, prefix)
    print(f"Found {len(keys)} JSON file(s) under s3://{bucket}/{prefix}")

    total = 0
    for key in keys:
        entity = _entity_from_key(key, prefix)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        # parse floats as Decimal so DynamoDB accepts them
        records = json.loads(body, parse_float=decimal.Decimal, parse_int=decimal.Decimal)
        if not isinstance(records, list):
            records = [records]

        with table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for idx, rec in enumerate(records):
                if not isinstance(rec, dict):
                    continue
                rec_id = _record_id(rec, idx)
                item = {"pk": f"{entity}#{rec_id}", "sk": entity, "entity": entity}
                for k, v in rec.items():
                    item[_clean_key(k)] = _clean_value(v)
                batch.put_item(Item=item)
                total += 1
        print(f"Seeded {len(records)} records for entity '{entity}' from {key}")

    print(f"Seeding complete: {total} total records written")


def _list_json_keys(bucket, prefix):
    """Return all *.json object keys under the prefix."""
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".json"):
                keys.append(obj["Key"])
    return keys


def _entity_from_key(key, prefix):
    """Derive an UPPER_SNAKE entity name from the file slug."""
    name = key[len(prefix):] if key.startswith(prefix) else key
    name = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _record_id(rec, idx):
    """Pick a stable id: first column whose name contains 'id', else the row index."""
    for k, v in rec.items():
        if "id" in k.lower() and v not in (None, ""):
            return str(v).strip()
    return str(idx)


def _clean_key(key):
    """DynamoDB attribute names can't be empty; normalize whitespace."""
    return str(key).strip() or "col"


def _clean_value(value):
    """Convert values into DynamoDB-safe types; drop NaN/empty."""
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value
