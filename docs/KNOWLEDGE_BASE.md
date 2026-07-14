# Knowledge Base (RAG) + Structured Data — Build-Time Ingestion

FAST can deploy a Bedrock Knowledge Base and a structured DynamoDB table, both
**seeded from the repo at deploy time** — no runtime upload. This is intended
for demos/POCs where you ship a fixed mock dataset alongside the agent.

Enable it in `infra-cdk/config.yaml`:

```yaml
backend:
  use_knowledge_base: true
```

Then deploy as usual (`cdk deploy`, or the CodeBuild path). Ingestion runs
during the deployment; the data is queryable the moment the stack completes.

## What gets built

| Resource | Purpose |
|---|---|
| S3 source bucket | Holds the deployed `mock-data/` contents |
| S3 Vectors bucket + index | Managed vector store (1024-dim, cosine) — cheapest option |
| Bedrock Knowledge Base + Data Source | RAG over the PDFs, Titan Text Embeddings v2 |
| `kb-ingest` custom resource | Runs `StartIngestionJob` at deploy and waits for completion |
| DynamoDB `<stack>-structured` table | Holds the structured datasets |
| `ddb-seed` custom resource | Loads `mock-data/structured/*.json` into DynamoDB at deploy |

## The dataset (`mock-data/`)

```
mock-data/
  knowledge/    # PDFs → Knowledge Base (RAG)
  structured/   # JSON records → DynamoDB (one file per dataset)
```

- **`knowledge/`** — any PDFs/text you want retrievable. The data source uses
  **`BEDROCK_FOUNDATION_MODEL` multimodal parsing**, so scanned/image PDFs are
  read by a foundation model (no separate OCR step). FM parsing falls back to
  the default parser per file, so a bad file never fails ingestion.
- **`structured/`** — each `*.json` is an array of record objects. The file
  slug becomes the entity name; each record is keyed
  `pk = "<ENTITY>#<id>"`, `sk = "<ENTITY>"`. To regenerate from XLSX, convert
  each sheet with pandas (`df.to_json(orient="records")`).

## Agent tools (strands-single-agent)

When the KB is deployed, the runtime receives `KNOWLEDGE_BASE_ID` and
`STRUCTURED_TABLE_NAME`, and the agent registers two tools:

- **`search_knowledge_base(query)`** — semantic retrieval over the documents;
  returns passages with their source for citation.
- **`query_structured_data(entity, filters, limit)`** — query CONTRACTORS /
  PROJECTS / PERMITS / INSPECTIONS by column filters.

Both are opt-in at runtime: the agent only registers a tool if its env var is
present, so disabling the KB cleanly removes the tools.

## Cost

S3 Vectors is pay-per-use (pennies at POC scale). The main variable cost is
Bedrock tokens: embeddings at ingest, the parsing model for scanned PDFs, and
`Retrieve` calls at query time. DynamoDB is on-demand.

## Re-ingesting

Both custom resources carry a timestamp property, so every `cdk deploy`
re-runs ingestion and re-seeds the table with whatever is in `mock-data/`.
