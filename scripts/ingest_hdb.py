#!/usr/bin/env python3
"""Stage the HDB mock dataset into S3 for the two retrieval lanes.

Phase 0 data-prep for the HDB knowledge-management chatbot (see
docs/HDB_KM_CHATBOT_ARCHITECTURE.md). This script does NOT create any AWS
resources — it only uploads prepared data into buckets you already have.

What it does
------------
1. Unstructured lane: uploads the policy / email / report PDFs into a raw-docs
   bucket under one S3 prefix per "space", and writes a Bedrock Knowledge Base
   metadata sidecar (``<file>.metadata.json``) next to each document so chunks
   can be filtered by ``space`` and access ``group`` at retrieval time.
2. Structured lane: converts the four Excel workbooks to Parquet (snake_cased
   columns) and uploads them, one prefix per table, into a tables bucket ready
   for a Glue crawler + Athena.

Spaces (kept verbatim from the dataset's "Query Mapping" sheet so the pipeline
stays traceable to the source labels).

Usage
-----
    python scripts/ingest_hdb.py \
        --dataset-dir /path/to/mock_dataset \
        --raw-bucket   my-hdb-raw-docs \
        --tables-bucket my-hdb-tables \
        [--region us-east-1] [--dry-run]

Requires: boto3, pandas, openpyxl, pyarrow  (see scripts/requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import boto3
import pandas as pd

# --- Space configuration ----------------------------------------------------
# Maps a source folder in the mock dataset to its space id (verbatim label),
# the S3 prefix used in the raw-docs bucket, and the Cognito access group that
# is entitled to it. Adjust the groups to match your Cognito user groups.


@dataclass(frozen=True)
class Space:
    folder: str  # folder name inside the dataset dir
    space_id: str  # verbatim label from the Query Mapping sheet
    group: str  # Cognito group entitled to this space


DOC_SPACES: list[Space] = [
    Space("SOPs & Policies", "HCSA-SOPs-and-Policies", "hdb-policies"),
    Space("Email Repository", "HCSA-Email-Repository", "hdb-emails"),
    Space("Reports", "HCSA-Reports", "hdb-reports"),
]

# Structured workbooks -> Athena table name. Sheet is auto-detected (first sheet).
STRUCTURED_FOLDER = "Structured Datasets"
STRUCTURED_TABLES: dict[str, str] = {
    "Contractor listing.xlsx": "contractors",
    "Development Projects.xlsx": "development_projects",
    "Permits.xlsx": "permits",
    "Inspections.xlsx": "inspections",
}


def _snake(name: str) -> str:
    """Normalize a column header to a SQL/Glue-friendly snake_case identifier."""
    s = str(name).strip().lower()
    s = re.sub(r"[()/]", " ", s)  # drop punctuation that breaks SQL identifiers
    s = re.sub(r"[^a-z0-9]+", "_", s)  # collapse runs of non-alnum to underscore
    return s.strip("_")


def _log(msg: str) -> None:
    print(msg, flush=True)


def upload_pdfs(
    s3, dataset_dir: Path, raw_bucket: str, dry_run: bool
) -> tuple[int, int]:
    """Upload PDFs + KB metadata sidecars, one S3 prefix per space."""
    uploaded = skipped = 0
    for space in DOC_SPACES:
        folder = dataset_dir / space.folder
        if not folder.is_dir():
            _log(f"  ! space folder missing, skipping: {folder}")
            continue
        pdfs = sorted(folder.glob("*.pdf"))
        _log(f"  [{space.space_id}] {len(pdfs)} PDF(s) -> s3://{raw_bucket}/{space.space_id}/")
        for pdf in pdfs:
            key = f"{space.space_id}/{pdf.name}"
            # Bedrock KB reads "<key>.metadata.json" for per-document metadata.
            meta = {
                "metadataAttributes": {
                    "space": space.space_id,
                    "group": space.group,
                    "source_file": pdf.name,
                }
            }
            if dry_run:
                _log(f"      would upload {key} (+ .metadata.json)")
            else:
                s3.upload_file(str(pdf), raw_bucket, key)
                s3.put_object(
                    Bucket=raw_bucket,
                    Key=f"{key}.metadata.json",
                    Body=json.dumps(meta).encode("utf-8"),
                    ContentType="application/json",
                )
            uploaded += 1
    return uploaded, skipped


def upload_tables(
    s3, dataset_dir: Path, tables_bucket: str, dry_run: bool
) -> int:
    """Convert each workbook to Parquet and upload under one prefix per table."""
    folder = dataset_dir / STRUCTURED_FOLDER
    if not folder.is_dir():
        _log(f"  ! structured folder missing: {folder}")
        return 0
    count = 0
    for filename, table in STRUCTURED_TABLES.items():
        src = folder / filename
        if not src.is_file():
            _log(f"  ! workbook missing, skipping: {src}")
            continue
        df = pd.read_excel(src, sheet_name=0, engine="openpyxl")
        df.columns = [_snake(c) for c in df.columns]
        key = f"{table}/{table}.parquet"
        _log(f"  [{table}] {len(df)} rows, cols={list(df.columns)}")
        if dry_run:
            _log(f"      would write s3://{tables_bucket}/{key}")
        else:
            local = folder / f".{table}.parquet"
            df.to_parquet(local, index=False)
            s3.upload_file(str(local), tables_bucket, key)
            local.unlink(missing_ok=True)
        count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage the HDB mock dataset into S3.")
    ap.add_argument("--dataset-dir", required=True, type=Path,
                    help="Path to the mock_dataset directory.")
    ap.add_argument("--raw-bucket", required=True,
                    help="S3 bucket for raw PDFs (unstructured lane / Bedrock KB source).")
    ap.add_argument("--tables-bucket", required=True,
                    help="S3 bucket for Parquet tables (structured lane / Glue + Athena).")
    ap.add_argument("--region", default=None, help="AWS region (defaults to your profile).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen without touching S3.")
    args = ap.parse_args()

    if not args.dataset_dir.is_dir():
        _log(f"error: dataset dir not found: {args.dataset_dir}")
        return 1

    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    s3 = session.client("s3")

    _log(f"== HDB data prep {'(DRY RUN)' if args.dry_run else ''} ==")
    _log("Unstructured lane (PDFs -> raw bucket):")
    up, _ = upload_pdfs(s3, args.dataset_dir, args.raw_bucket, args.dry_run)
    _log("Structured lane (xlsx -> Parquet -> tables bucket):")
    tbls = upload_tables(s3, args.dataset_dir, args.tables_bucket, args.dry_run)

    _log(f"\nDone: {up} document(s) staged, {tbls} table(s) written.")
    if not args.dry_run:
        _log("Next: run the Bedrock KB ingestion job and the Glue crawler (Phase 1 / 2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
