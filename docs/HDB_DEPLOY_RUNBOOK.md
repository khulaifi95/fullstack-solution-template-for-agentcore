# HDB Deploy Runbook (Phases 0–2)

End-to-end steps to deploy the FAST baseline **plus** the two HDB retrieval lanes
(Bedrock KB + OpenSearch, Glue + Athena), stage the mock dataset, and verify that
**vector search** and **Athena SQL** actually return results.

Run this from the repo root on a machine with your AWS credentials. Unlike the
earlier phases, these steps need real AWS access + Docker — they cannot run in the
background agent environment.

> **Region requirement:** the KB's FM-based advanced parsing uses a **US**
> cross-region inference profile (`us.anthropic.claude-sonnet-4-...`). Deploy in a
> **US region** (`us-east-1` or `us-west-2`). To deploy elsewhere, set
> `knowledge_base.advanced_parsing: false` or change `parsing_model` in config.yaml
> and adjust the profile region in `lib/knowledge-base-construct.ts`.

---

## 0. Prerequisites (once)

```bash
aws sts get-caller-identity          # confirms credentials work
aws configure get region             # note your region (must be US — see above)
docker info >/dev/null && echo OK    # Docker must be running (FAST bundles Python Lambdas)
npm install -g aws-cdk               # or use npx from infra-cdk
```

**Enable Bedrock model access** (Console → Bedrock → Model access) in your region for:
- `amazon.titan-embed-text-v2:0` (embeddings)
- `anthropic.claude-sonnet-4-*` (advanced parsing) — the model behind the US inference profile

Ingestion fails if these aren't enabled.

The two lanes are already turned on in `infra-cdk/config.yaml`
(`knowledge_base:` and `structured_data:` sections are uncommented, and both let
CDK create their S3 buckets).

---

## 1. Deploy the stack

```bash
cd infra-cdk
npm install                 # includes @cdklabs/generative-ai-cdk-constructs
cdk bootstrap               # once per account+region, ever
cdk deploy                  # provisions baseline + KB + OpenSearch + Glue + Athena
cd ..
```

This creates (among the FAST baseline resources):
- Bedrock Knowledge Base + OpenSearch Serverless collection/index
- KB **source bucket** (unstructured)
- Glue database `hdb_structured` + crawler, Athena workgroup + results bucket
- **tables bucket** (structured)

**Capture the outputs** (also visible in the CloudFormation console):

```bash
aws cloudformation describe-stacks --stack-name FAST-stack \
  --query "Stacks[0].Outputs[?contains(OutputKey,'KnowledgeBase') || contains(OutputKey,'Structured') || contains(OutputKey,'Glue') || contains(OutputKey,'Athena')].{K:OutputKey,V:OutputValue}" \
  --output table
```

Note these values — you'll use them below:
- `KnowledgeBaseSourceBucket`
- `StructuredTablesBucket`
- `GlueCrawlerName`
- `GlueDatabaseName` (`hdb_structured`)
- `AthenaWorkgroup`

Deploy the frontend too (unchanged from FAST):

```bash
python scripts/deploy-frontend.py
```

---

## 2. Stage the dataset into S3

```bash
pip install -r scripts/requirements.txt

python scripts/ingest_hdb.py \
  --dataset-dir "/path/to/mock_dataset" \
  --raw-bucket    <KnowledgeBaseSourceBucket> \
  --tables-bucket <StructuredTablesBucket>
```

Expect: 62 documents (3 space prefixes, each PDF + a `.metadata.json`) and 4 Parquet
tables.

---

## 3. Make the structured lane queryable (run the crawler)

```bash
aws glue start-crawler --name <GlueCrawlerName>

# Wait until state READY (crawler takes ~1-2 min on this dataset):
aws glue get-crawler --name <GlueCrawlerName> --query "Crawler.State" --output text
```

Verify tables + a real join in Athena:

```bash
aws athena start-query-execution \
  --work-group <AthenaWorkgroup> \
  --query-string "SELECT table_name FROM information_schema.tables WHERE table_schema='hdb_structured'"

# Fused query — contractors whose projects failed inspection (case-insensitive!):
aws athena start-query-execution \
  --work-group <AthenaWorkgroup> \
  --query-string "SELECT c.contractor_name, COUNT(*) n
    FROM inspections i
    JOIN development_projects p ON i.case_id = p.project_id
    JOIN contractors c ON p.contractor_id = c.contractor_id
    WHERE LOWER(i.inspection_result) LIKE '%fail%'
    GROUP BY c.contractor_name ORDER BY n DESC LIMIT 5"
```

Then `aws athena get-query-execution --query-execution-id <id>` → get the S3 result,
or view results in the Athena console. Expect ~161 failed-inspection rows total.

> **Data-quality note baked into the Phase 3 SQL prompt:** `inspection_result` has
> mixed casing/values (`PASS`/`Pass`/`FAIL`/`Fail`/`PASS_WITH_ADVISORY`/
> `Conditional Pass`) — always match case-insensitively. `contractors` has some
> duplicate `contractor_id`s (project→contractor join fans out slightly).

---

## 4. Make the unstructured lane queryable (KB ingestion job)

The KB is created empty. Start an ingestion job so PDFs are parsed → chunked →
embedded → indexed. Get the data source id first:

```bash
KB_ID=$(aws ssm get-parameter --name /FAST-stack/knowledge-base/id --query Parameter.Value --output text)
DS_ID=$(aws bedrock-agent list-data-sources --knowledge-base-id "$KB_ID" \
  --query "dataSourceSummaries[0].dataSourceId" --output text)

aws bedrock-agent start-ingestion-job --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID"
```

Poll until `COMPLETE` (FM advanced parsing of the reports makes this the slow step —
several minutes):

```bash
aws bedrock-agent list-ingestion-jobs --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" \
  --query "ingestionJobSummaries[0].{status:status,docs:statistics.numberOfDocumentsScanned}"
```

> Querying **before** ingestion completes returns empty results — this is expected.

---

## 5. Verify vector search returns cited results

```bash
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "$KB_ID" \
  --retrieval-query '{"text":"What should a worker do if a confined space entry permit has expired?"}' \
  --query "retrievalResults[].{score:score,source:location.s3Location.uri,page:metadata.\"x-amz-bedrock-kb-document-page-number\"}" \
  --output table
```

Expect chunks from `SOP-CO-003.pdf` / `POL-CO-003.pdf` with scores and (for reports)
page numbers — confirming citations carry through.

Test a metadata-filtered retrieve (access control — only the reports space):

```bash
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "$KB_ID" \
  --retrieval-query '{"text":"financial risk categories"}' \
  --retrieval-configuration '{"vectorSearchConfiguration":{"filter":{"equals":{"key":"space","value":"HCSA-Reports"}}}}'
```

---

## Exit checklist (ready for Phase 3)

- [ ] `cdk deploy` succeeded; outputs captured.
- [ ] Frontend deployed; baseline chat works.
- [ ] 62 docs + 4 Parquet tables staged in S3.
- [ ] Glue crawler READY; Athena join query returns rows.
- [ ] KB ingestion job COMPLETE.
- [ ] `retrieve` returns scored chunks **with source + page**; metadata filter works.

Once all boxes are checked, both lanes are live and we build the `retrieve` /
`run_sql` MCP tools (Phase 3) against the real KB id + Athena workgroup.
