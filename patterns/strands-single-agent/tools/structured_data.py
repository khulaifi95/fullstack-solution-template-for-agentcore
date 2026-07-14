# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured-data query tool for the Strands agent (DynamoDB mock datasets)."""

import decimal
import json
import os

import boto3
from boto3.dynamodb.conditions import Attr
from strands import tool

# Entity names match the UPPER_SNAKE slugs written by the ddb-seed Lambda.
_ENTITIES = [
    "CONTRACTOR_LISTING_CONTRACTORS",
    "DEVELOPMENT_PROJECTS_DEVELOPMENT_PROJECTS",
    "INSPECTIONS_INSPECTIONS",
    "PERMITS_PERMITS",
]
_MAX_ITEMS = 100


def _table():
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    name = os.environ["STRUCTURED_TABLE_NAME"]
    return boto3.resource("dynamodb", region_name=region).Table(name)


def _json(obj) -> str:
    def default(o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return str(o)

    return json.dumps(obj, default=default)


@tool
def query_structured_data(entity: str, filters: str = "", limit: int = 50) -> str:
    """Query the structured HCSA/HDB datasets (contractors, projects, permits, inspections).

    Use this for questions about the tabular data — counts, statuses, ratings,
    costs, dates, or joining projects to their permits/inspections/contractors.
    Available entities: 'CONTRACTORS', 'PROJECTS', 'PERMITS', 'INSPECTIONS'.

    Args:
        entity: Which dataset to query — one of CONTRACTORS, PROJECTS, PERMITS, INSPECTIONS.
        filters: Optional JSON object of {column: value} to match (case-insensitive
                 substring match on string columns). Example: '{"Engagement Status": "SUSPENDED"}'.
        limit: Max records to return (default 50, capped at 100). The total match
               count is always returned regardless of limit.

    Returns:
        JSON string with {entity, count, returned, items}.
    """
    key = _resolve_entity(entity)
    if not key:
        return _json({"error": f"Unknown entity '{entity}'. Use one of: CONTRACTORS, PROJECTS, PERMITS, INSPECTIONS."})

    parsed_filters = {}
    if filters:
        try:
            parsed_filters = json.loads(filters) if isinstance(filters, str) else dict(filters)
        except (ValueError, TypeError):
            return _json({"error": f"filters must be a JSON object, got: {filters!r}"})

    try:
        items = _query_entity(key)
    except Exception as exc:  # noqa: BLE001
        return _json({"error": f"Query failed: {exc}"})

    matched = [it for it in items if _matches(it, parsed_filters)]
    capped = min(int(limit or 50), _MAX_ITEMS)
    return _json(
        {
            "entity": key,
            "count": len(matched),
            "returned": min(len(matched), capped),
            "items": [_strip(it) for it in matched[:capped]],
        }
    )


def _resolve_entity(entity: str):
    e = (entity or "").strip().upper()
    for full in _ENTITIES:
        if e in full or full.startswith(e):
            return full
    aliases = {
        "CONTRACTORS": "CONTRACTOR_LISTING_CONTRACTORS",
        "CONTRACTOR": "CONTRACTOR_LISTING_CONTRACTORS",
        "PROJECTS": "DEVELOPMENT_PROJECTS_DEVELOPMENT_PROJECTS",
        "PROJECT": "DEVELOPMENT_PROJECTS_DEVELOPMENT_PROJECTS",
        "PERMITS": "PERMITS_PERMITS",
        "PERMIT": "PERMITS_PERMITS",
        "INSPECTIONS": "INSPECTIONS_INSPECTIONS",
        "INSPECTION": "INSPECTIONS_INSPECTIONS",
    }
    return aliases.get(e)


def _query_entity(entity: str):
    """Fetch all rows for an entity by filtering on the sort key (sk == entity).

    sk is the table's sort key (not a partition key), so a filtered scan is used.
    The datasets are small — a few hundred rows each — so this is inexpensive.
    """
    table = _table()
    items = []
    scan_kwargs = {"FilterExpression": Attr("sk").eq(entity)}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        scan_kwargs["ExclusiveStartKey"] = lek
    return items


def _matches(item, filters) -> bool:
    for col, val in filters.items():
        actual = item.get(col)
        if actual is None:
            # try case-insensitive column match
            actual = next((v for k, v in item.items() if k.lower() == str(col).lower()), None)
        if actual is None:
            return False
        if str(val).strip().lower() not in str(actual).strip().lower():
            return False
    return True


def _strip(item):
    """Drop internal keys from returned records."""
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "entity")}
