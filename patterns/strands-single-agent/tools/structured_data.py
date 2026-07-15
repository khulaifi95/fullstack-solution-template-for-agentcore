# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured-data query tool for the Strands agent (DynamoDB mock datasets)."""

import decimal
import json
import os
from urllib.parse import quote

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import ClientError
from strands import tool

# Entity names match the UPPER_SNAKE slugs written by the ddb-seed Lambda.
_ENTITIES = [
    "CONTRACTOR_LISTING_CONTRACTORS",
    "DEVELOPMENT_PROJECTS_DEVELOPMENT_PROJECTS",
    "INSPECTIONS_INSPECTIONS",
    "PERMITS_PERMITS",
]
_MAX_ITEMS = 100
_LINK_TTL = 3600

# Each entity's originating source dataset — used to cite structured answers,
# mirroring how KB answers cite their PDF. display = the name graders expect;
# key = the object under the KB source bucket's structured/ prefix.
_SOURCE = {
    "CONTRACTOR_LISTING_CONTRACTORS": ("Contractor listing.xlsx", "structured/contractor-listing-contractors.json"),
    "DEVELOPMENT_PROJECTS_DEVELOPMENT_PROJECTS": ("Development Projects.xlsx", "structured/development-projects-development-projects.json"),
    "INSPECTIONS_INSPECTIONS": ("Inspections.xlsx", "structured/inspections-inspections.json"),
    "PERMITS_PERMITS": ("Permits.xlsx", "structured/permits-permits.json"),
}


def _table():
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    name = os.environ["STRUCTURED_TABLE_NAME"]
    return boto3.resource("dynamodb", region_name=region).Table(name)


def _source_ref(entity: str) -> dict:
    """Return {source, link} for an entity's originating dataset (link best-effort)."""
    display, key = _SOURCE.get(entity, (entity, ""))
    ref = {"source": display, "link": ""}
    bucket = os.environ.get("KB_SOURCE_BUCKET")
    if bucket and key:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        try:
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=f"https://s3.{region}.amazonaws.com",
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            )
            ref["link"] = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=_LINK_TTL,
            )
        except (ClientError, Exception):  # noqa: BLE001 - link is best-effort
            pass
    return ref


def _json(obj) -> str:
    def default(o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return str(o)

    return json.dumps(obj, default=default)


# Categorical columns are those with few distinct values; used by
# describe_structured_data to surface the enum vocabulary for NL mapping.
_MAX_CARDINALITY_FOR_ENUM = 25


@tool
def describe_structured_data(entity: str = "") -> str:
    """Describe the schema of the structured HCSA/HDB datasets for query planning.

    Call this FIRST when a question uses fuzzy or natural wording (e.g. "troubled
    contractors", "overdue permits", "poorly rated builders") so you can map it to
    the real column names and their exact allowed values before querying. Returns,
    per entity, the list of columns and — for low-cardinality (categorical) columns —
    the distinct values that actually occur in the data.

    Args:
        entity: Optional — one of CONTRACTORS, PROJECTS, PERMITS, INSPECTIONS.
                Omit to describe all four.

    Returns:
        JSON string: {entities: {ENTITY: {row_count, columns, categoricals: {col: [values]}}}}.
    """
    targets = _ENTITIES
    if entity:
        key = _resolve_entity(entity)
        if not key:
            return _json({"error": f"Unknown entity '{entity}'. Use CONTRACTORS, PROJECTS, PERMITS, INSPECTIONS."})
        targets = [key]

    out = {}
    for ent in targets:
        try:
            items = _query_entity(ent)
        except Exception as exc:  # noqa: BLE001
            out[ent] = {"error": str(exc)}
            continue
        columns = set()
        values_by_col = {}
        for it in items:
            for col, val in _strip(it).items():
                columns.add(col)
                values_by_col.setdefault(col, set()).add(str(val).strip())
        # Drop empty / null-ish placeholders so the agent sees clean vocabulary.
        _skip = {"", "none", "nan", "null"}
        categoricals = {}
        for col, vals in values_by_col.items():
            clean = sorted(v for v in vals if v and v.strip().lower() not in _skip)
            if 1 < len(clean) <= _MAX_CARDINALITY_FOR_ENUM:
                categoricals[col] = clean
        out[ent] = {
            "row_count": len(items),
            "columns": sorted(columns),
            "categoricals": categoricals,
        }
    return _json({"entities": out})


@tool
def query_structured_data(entity: str, filters: str = "", limit: int = 50) -> str:
    """Query the structured HCSA/HDB datasets (contractors, projects, permits, inspections).

    Use this for questions about the tabular data — counts, statuses, ratings,
    costs, dates, or joining projects to their permits/inspections/contractors.
    Available entities: 'CONTRACTORS', 'PROJECTS', 'PERMITS', 'INSPECTIONS'.

    For fuzzy/natural-language questions, first call `describe_structured_data`
    to learn the exact column names and allowed values, then translate the
    question into precise filters here. A filter value may be a single string OR
    a list of strings (matches ANY — logical OR), which lets you map a fuzzy term
    to several exact values, e.g. "troubled contractors" ->
    {"Financial Health Rating": ["UNDER_REVIEW", "FAIR"]}.

    Args:
        entity: Which dataset to query — one of CONTRACTORS, PROJECTS, PERMITS, INSPECTIONS.
        filters: Optional JSON object of {column: value | [values]}. String values match
                 case-insensitively (substring); a list matches ANY of its values.
                 Example: '{"Engagement Status": "SUSPENDED"}' or
                 '{"Contractor Rating": ["GOLD", "PLATINUM"]}'.
        limit: Max records to return (default 50, capped at 100). The total match
               count is always returned regardless of limit.

    Returns:
        JSON string with {entity, count, returned, items, source, link}. Cite
        `source` in your answer as a markdown link using `link` (a temporary URL
        to the originating dataset), and include it in the References section.
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
    ref = _source_ref(key)
    return _json(
        {
            "entity": key,
            "count": len(matched),
            "returned": min(len(matched), capped),
            "items": [_strip(it) for it in matched[:capped]],
            # Cite this in the answer as a markdown link, like KB sources.
            "source": ref["source"],
            "link": ref["link"],
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
    """AND across columns; a list value means OR across its options (substring, case-insensitive)."""
    for col, val in filters.items():
        actual = item.get(col)
        if actual is None:
            # try case-insensitive column match
            actual = next((v for k, v in item.items() if k.lower() == str(col).lower()), None)
        if actual is None:
            return False
        haystack = str(actual).strip().lower()
        options = val if isinstance(val, list) else [val]
        if not any(str(opt).strip().lower() in haystack for opt in options):
            return False
    return True


def _strip(item):
    """Drop internal keys from returned records."""
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "entity")}
