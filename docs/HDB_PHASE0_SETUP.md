# HDB Phase 0 — Foundations & Data Prep

This is the runbook for **Phase 0** of the HDB knowledge-management chatbot
(see `HDB_KM_CHATBOT_ARCHITECTURE.md`). Phase 0 has two parts:

- **Part A** — deploy the FAST baseline and confirm it works. *(You run this; it
  needs your AWS credentials.)*
- **Part B** — stage the mock dataset into S3 with `scripts/ingest_hdb.py`.

At the end of Phase 0 you'll have a working FAST stack plus the dataset staged in
S3, ready for the Bedrock KB (Phase 1) and Glue/Athena (Phase 2) lanes.

---

## Prerequisites

- AWS account + credentials configured (`aws sts get-caller-identity` should
  succeed). If not, run `aws login` / configure a profile first.
- Node.js 18+ and the AWS CDK CLI: `npm install -g aws-cdk`
- Python 3.9+ and `pip`
- The mock dataset directory (the folder containing `SOPs & Policies/`,
  `Email Repository/`, `Reports/`, `Structured Datasets/`).

---

## Part A — Deploy the FAST baseline

This provisions Cognito, the AgentCore Runtime + Gateway, Memory, and Amplify
hosting — the substrate the new lanes plug into.

```bash
cd infra-cdk
npm install
cdk bootstrap          # once per account+region, ever
cdk deploy             # provisions the baseline stack
cd ..
python scripts/deploy-frontend.py   # builds + deploys the React app to Amplify
```

Notes:
- Optionally set `admin_user_email` in `infra-cdk/config.yaml` before `cdk deploy`
  to auto-create an admin Cognito user and email the credentials.
- `config.yaml` defaults (`pattern: strands-single-agent`, `network_mode: PUBLIC`)
  are fine for Phase 0.

### Confirm it works

```bash
python test-scripts/test-agent.py       # exercises the deployed agent runtime
```

Open the Amplify URL from the CDK/Amplify outputs, log in with your Cognito user,
and send a chat message. If the agent replies, the baseline is healthy.

---

## Part B — Stage the mock dataset into S3

Phase 0's data prep only needs two buckets. Bedrock KB / OpenSearch / Glue /
Athena come in Phases 1–2 — for now we just land the data.

### 1. Create the two staging buckets

Replace `<ACCT>`/`<REGION>` with your values (bucket names are globally unique):

```bash
aws s3 mb s3://hdb-raw-docs-<ACCT>-<REGION>     # unstructured lane (PDFs)
aws s3 mb s3://hdb-tables-<ACCT>-<REGION>       # structured lane (Parquet)
```

> When we build the Phase 1/2 CDK constructs, these buckets will be created and
> named by CDK instead; the manual buckets here are only to unblock data prep.

### 2. Install script deps and run

```bash
pip install -r scripts/requirements.txt

# Dry run first — prints what would happen, touches nothing:
python scripts/ingest_hdb.py \
  --dataset-dir "/path/to/mock_dataset" \
  --raw-bucket   hdb-raw-docs-<ACCT>-<REGION> \
  --tables-bucket hdb-tables-<ACCT>-<REGION> \
  --dry-run

# Real run:
python scripts/ingest_hdb.py \
  --dataset-dir "/path/to/mock_dataset" \
  --raw-bucket   hdb-raw-docs-<ACCT>-<REGION> \
  --tables-bucket hdb-tables-<ACCT>-<REGION>
```

### What the script produces

**Unstructured lane** — one S3 prefix per space, each PDF paired with a Bedrock
KB metadata sidecar:

```
s3://<raw>/HCSA-SOPs-and-Policies/SOP-CO-003.pdf
s3://<raw>/HCSA-SOPs-and-Policies/SOP-CO-003.pdf.metadata.json
s3://<raw>/HCSA-Email-Repository/Email 41.pdf              (+ .metadata.json)
s3://<raw>/HCSA-Reports/HDB-AR-FY23.pdf                    (+ .metadata.json)
```

Each sidecar tags the document for access-controlled retrieval:

```json
{ "metadataAttributes": {
    "space": "HCSA-SOPs-and-Policies",
    "group": "hdb-policies",
    "source_file": "SOP-CO-003.pdf" } }
```

(Space ids are kept verbatim from the dataset's `Query Mapping` sheet. Edit the
`group` values in `scripts/ingest_hdb.py` to match your Cognito user groups.)

**Structured lane** — one Parquet table per workbook, snake_cased columns:

```
s3://<tables>/contractors/contractors.parquet            (253 rows)
s3://<tables>/development_projects/development_projects.parquet (289 rows)
s3://<tables>/permits/permits.parquet                    (317 rows)
s3://<tables>/inspections/inspections.parquet            (297 rows)
```

### Data model (for the Phase 2 text-to-SQL prompt)

`project_id` (values like `UP-2018-001`) is the hub key:

- `development_projects.contractor_id` → `contractors.contractor_id` (`CONTR-*`)
- `permits.assignment_id` → `development_projects.project_id` *(differently named
  column, but holds `UP-*` project ids)*
- `inspections.case_id` → `development_projects.project_id` *(same — holds `UP-*`)*

These non-obvious join keys must be spelled out in the text-to-SQL system prompt so
the agent joins on the right columns.

---

## Phase 0 exit checklist

- [ ] `cdk deploy` succeeded; agent replies via `test-agent.py` and the Amplify UI.
- [ ] 62 documents + metadata sidecars in the raw bucket (3 space prefixes).
- [ ] 4 Parquet tables in the tables bucket.
- [ ] Ready for **Phase 1** (Bedrock KB + OpenSearch construct) and **Phase 2**
      (Glue crawler + Athena).
