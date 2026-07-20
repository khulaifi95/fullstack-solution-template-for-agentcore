# HCSA Knowledge-Management Chatbot — Architecture & Build Plan

> **Status:** Design (pre-implementation). Built on the FAST (Fullstack AgentCore Solution
> Template) baseline. This document is the reference for the "updated architecture" of the
> HDB/HCSA prototype.

## 1. Context

This is a tender-submission prototype for **HCSA** (Housing, Construction & Sustainability
Authority — a fictional agency standing in for HDB; per the brief, HDB and HCSA are treated
as one entity). The deliverable is an **AI-driven LLM knowledge-management chatbot** that lets
officers retrieve information across a fragmented document estate instead of manually searching
folders.

The system is **RAG-first** (retrieval-augmented generation), not a generic tool-calling
assistant. It must answer natural-language questions grounded **only** in the provided corpus,
with **accurate, page-level source citations**, and is scored at interview on a hidden test set
across five KPIs: **accuracy, recall, precision, completeness, faithfulness** (see the Prototype
Instructions, Annex A).

### Knowledge base — four source "spaces"

The mock dataset (and the sample-query mapping) organizes content into four spaces. These map
directly onto our two retrieval lanes and onto the access-control model.

| Space | Content | Format | Lane |
|---|---|---|---|
| `HCSA-SOPs-and-Policies` | Policies, SOPs (POL-*, SOP-*) | PDF | Unstructured (vector) |
| `HCSA-Email-Repository` | ~50 emails (correspondence, approvals) | PDF | Unstructured (vector) |
| `HCSA-Reports` | Financial statements & annual reports | PDF (page-cited) | Unstructured (vector) |
| `HCSA-Structured-Data` | Contractors, Development Projects, Permits, Inspections | XLSX (relational) | Structured (SQL) |

### What the sample queries tell us (29 queries analysed)

- **Distribution:** Workplace Safety 9, Annual Report 6, **Structured data 9**, **Workplace
  Safety + Structured data 2**, Sustainability 2, Permits 1. **~38% of queries touch the
  structured lane**, and **2 require fusing SQL results with policy text in a single answer** —
  so multi-tool orchestration and result fusion are first-class requirements.
- **Answer style:** grounded, moderately detailed prose that closely paraphrases source clauses —
  complete but not verbose (the brief penalizes excess length and irrelevant content). Each
  expected answer has a counted number of "key points" that completeness is scored against.
- **Citation granularity is page-level:** e.g. `SOP-CO-003.pdf`, `POL-CO-003.pdf` (whole file),
  `HDB FS-22.pdf Page 36 / Page 37 / Page 47` (reports cited by **specific page**),
  `Email 41.pdf` (individual email), `Contractor listing.xlsx` (structured source). **The
  retriever must preserve and surface page-number metadata.**

## 2. Target architecture

```
              INGESTION (batch / on upload)                RETRIEVAL (per query)             APP
┌─ Unstructured lane ──────────────────────────┐
│  Policies · SOPs · Emails · Reports (PDF)     │
│        │ upload / seed                        │
│        ▼                                      │
│   S3 (raw docs, per-space prefixes)           │
│        │  StartIngestionJob                   │
│        ▼                                      │
│   Bedrock Knowledge Base ────────────────────►│  OpenSearch Serverless   ◄─ retrieve tool ─┐
│     parse (FM/advanced) → chunk (hierarchical)│  (vector index +                           │
│     → embed (Titan V2 1024)                   │   metadata: space, page, group)            │
└───────────────────────────────────────────────┘                                           │
                                                                                    ┌────────┴─────────┐
┌─ Structured lane ────────────────────────────┐                                    │ AgentCore Gateway │
│  4 Excel files                               │                                    │  (MCP tools)      │
│        │ xlsx → Parquet (prep script)         │                                   │  • retrieve       │
│        ▼                                      │                                    │  • run_sql        │
│   S3 (tables) ─► Glue Data Catalog ◄─ Athena ►│  run_sql tool ──────────────────── │  Cedar authz      │
│                     (schema)     (SQL engine) │                                    └────────┬─────────┘
└───────────────────────────────────────────────┘                                            │ MCP (M2M OAuth,
                                                                                     ┌─────────┴──────────┐ user identity
                                                                                     │  AgentCore Runtime  │ propagated)
                                                                                     │  RAG orchestrator   │
                                                                                     │  (Strands agent)    │
                                                                                     │  + Guardrails       │
                                                                                     │  + Memory           │
                                                                                     └─────────┬──────────┘
                                                                                     /invocations (SigV4, streaming)
                                                                                     ┌─────────┴──────────┐
                                                                                     │  FAST React frontend│ ◄─ officer
                                                                                     │  (Amplify hosting)  │
                                                                                     └────────────────────┘

Cross-cutting: AgentCore Evaluation · Observability (traces) · Memory · Bedrock Guardrails
Identity: Cognito User Pool (login + group membership) → Cedar policies at Gateway
```

The frontend is **FAST's existing React app** (not Open WebUI). This keeps a single stack,
lets identity flow natively to AgentCore (no OpenAI-protocol adapter shim), and gives us a home
for the many mandated admin/eval pages (Annex B) that no off-the-shelf chat UI provides.

## 3. Component decisions (these are the choices the proposal must justify)

| Concern | Choice | Rationale |
|---|---|---|
| **Vector store** | OpenSearch Serverless (via Bedrock KB) | Strongest metadata filtering (needed to scope retrieval by space/group/page for defensible citations), hybrid semantic+keyword search (boosts recall & precision), can adopt binary vectors later for cost. Quick-create supported. |
| **Embedding model** | **Titan Text Embeddings V2 @ 1024-dim float** (`amazon.titan-embed-text-v2:0`) | 8,192-token context handles long policy/report sections without over-fragmenting; full 1024-dim float maximizes recall & citation fidelity. Configurable dims (256/512) + binary available as a later cost lever. Switch to `cohere.embed-multilingual-v3` only if corpus turns heavily non-English (accepting its 512-token limit → finer chunks). |
| **Ingestion (unstructured)** | Bedrock KB native `StartIngestionJob` + **FM-based advanced parsing** | KB owns parse→chunk→embed→index. Advanced (foundation-model) parsing preserves tables/structured sections in the financial reports so page-level facts survive chunking. |
| **Chunking** | **Hierarchical chunking** (configurable) | Preserves clause/section structure → better page-anchored citations and completeness. Strategy, size, overlap, and parser are exposed as configuration (customer requirement #1 and the tender's "justify your chunking strategy"). |
| **Reranking** | Query-time reranker (e.g. Cohere/Amazon rerank) | Lifts the most relevant chunks to the top → better attribution and precision. |
| **Structured querying** | Athena text-to-SQL over Glue-catalogued Parquet | True relational joins across Contractors↔Projects↔Permits↔Inspections. Vector RAG cannot answer joins like "which projects by contractor X failed inspection." |
| **xlsx → Parquet** | Small pandas prep script (not Glue ETL) | Only 4 small files; a Glue crawler then catalogs the Parquet. |
| **LLM** | Claude on Bedrock (Opus 4.8 / Sonnet 5) | Best at grounded, cited synthesis, reliable text-to-SQL, and multi-tool orchestration. |
| **Guardrails** | Bedrock Guardrails | Enforce "answer only from the provided dataset" and block ungrounded claims → protects the faithfulness KPI. |
| **Orchestrator** | FAST Strands agent on AgentCore Runtime | Routes retrieve vs. run_sql vs. both, fuses results, assembles citations. Already wired for Gateway M2M auth + Memory. |
| **Frontend** | FAST React app on Amplify | Single stack; native identity propagation; home for the Annex B admin/eval pages. |

## 4. Access control (customer requirement #4)

Group-based access control keyed off the Cognito User Pool and enforced at the Gateway with
Cedar — the mechanism FAST already ships.

- **Spaces are resources.** Each of the four spaces (and, where needed, individual documents)
  is an access-controlled resource.
- **Retrieval-time enforcement:** every chunk is tagged in the KB with `space` and an access
  `group` in its metadata. The `retrieve` tool applies a **metadata filter** derived from the
  caller's Cognito groups, so users only ever retrieve from spaces they are entitled to.
- **Upload-time enforcement:** the File-upload / KB-management flow only permits writing into a
  space the user's group is authorized for; Cedar policies at the Gateway gate the `upload`
  action, and the uploaded chunks inherit the space's `group` tag.
- **Identity propagation:** the user's identity is carried from the validated JWT into the M2M
  token (via the existing Cognito V3 pre-token Lambda), so Cedar and the KB filter both evaluate
  against the real caller — never a value from the request payload.

## 5. Accuracy & citation strategy (customer requirement #3 — the north star)

Accuracy and faithful, precise citations are prioritized above all else:

1. **Retrieval quality:** hybrid search + hierarchical chunking + FM parsing + reranking →
   maximize that the right paragraphs are retrieved (recall/precision).
2. **Page-anchored citations:** preserve `x-amz-bedrock-kb-document-page-number` (and source
   filename/space) through the `retrieve` tool into the response. Report answers must cite the
   specific page, matching the expected-answer format.
3. **Grounded generation:** system prompt requires the agent to answer only from retrieved
   context, cite every claim, and stay complete-but-concise.
4. **Faithfulness guardrail:** Bedrock Guardrails + a citation-verification step reject or flag
   claims not supported by retrieved evidence before the answer is returned.

## 6. Mapping to FAST (what to add / change)

| Need | FAST provides | Delta to build |
|---|---|---|
| Chat UI, login, admin pages | React/shadcn app, Cognito, Amplify | Build Annex B pages (chat + KB mgmt, file upload, eval dashboard, system consumption, config, etc.) |
| Agent runtime | AgentCore Runtime + Strands pattern | Recast agent as RAG orchestrator + citation assembly + guardrails |
| Vector RAG lane | — (baseline gap) | Bedrock KB + OpenSearch + ingestion job (new CDK construct) |
| Structured lane | DynamoDB (wrong shape) | Glue + Athena + Parquet tables (new CDK construct) |
| Tools behind Gateway | Gateway + Cedar + sample tool | Replace sample tool with `retrieve` + `run_sql` MCP tools |
| Access control | Cognito groups + Cedar | Space/group metadata tagging + retrieval metadata filter + upload authz |
| Eval harness | AgentCore Evaluation + Observability | 5-KPI harness over Annex-C queries + dashboard pages |

## 7. Phased build plan

**Phase 0 — Foundations & data prep**
- Deploy FAST baseline; confirm Runtime/Gateway/Cognito/Memory work.
- Prep script: PDFs → S3 raw bucket (per-space prefixes); 4 xlsx → Parquet → S3 tables bucket.

**Phase 1 — Unstructured lane (Bedrock KB)**
- New CDK construct `lib/knowledge-base-construct.ts`: S3 data source + Bedrock KB +
  OpenSearch Serverless collection + hierarchical chunking config + Titan V2 embeddings +
  advanced parsing. Metadata schema: `space`, `page`, `group`, `source_file`.
- Run ingestion; validate retrieval + page citations on Annex-C sample queries.

**Phase 2 — Structured lane (Glue + Athena)**
- New CDK construct `lib/structured-data-construct.ts`: Glue database + crawler over Parquet,
  Athena workgroup + results bucket, IAM.
- Validate FK joins (project→contractor→permits→inspections) with hand-written SQL.

**Phase 3 — Retrieval tools on Gateway**
- Two MCP tools in `gateway/tools/`: `retrieve` (KB `Retrieve` API → chunks + source/page/space
  metadata, filtered by caller groups) and `run_sql` (Athena execution).
- Register on Gateway; Cedar policies gate by space/group.

**Phase 4 — Agent → RAG orchestrator**
- Rework `patterns/strands-single-agent/basic_agent.py`: grounded/cited/concise system prompt;
  routing between `retrieve` / `run_sql` / both; citation assembly; attach Guardrails.

**Phase 5 — Frontend (Annex B pages)**
- Chat with inline page-level citations; File upload (space-scoped); KB management; Conversation
  history; plus mandated mockups (document generation, user management, chatbot config, system
  consumption).

**Phase 6 — Evaluation & dashboards (the scored core)**
- 5-KPI harness (accuracy, recall, precision, completeness, faithfulness) over Annex-C queries,
  reading the Annex-C columns as ground truth; LLM-as-judge where semantic matching is needed.
- Pages: Automated query testing & response evaluation; System performance dashboard; System
  consumption — fed from AgentCore Evaluation + Observability traces.

**Phase 7 — Hardening for interview**
- Guardrail tuning, latency (scored as "high response performance"), robustness on hidden set,
  remaining mockups.

## 8. Open questions

1. **IaC scope:** extend `infra-cdk` for both new lanes (recommended, single deploy).
2. **Reranker availability** in the target region — confirm before committing Phase 1.
3. **Advanced-parsing cost** vs. default parsing — validate the reports actually need FM parsing
   (they likely do, given page-level financial-table citations).
4. **Eval ground truth:** confirm the harness reads Annex-C columns (relevant paragraphs, key
   points, citation sources) directly as the labeled set.

## 9. Appendix — supported Bedrock KB embedding models (early 2026)

| Model | Model ID | Dims | Max input | Multilingual | Notes |
|---|---|---|---|---|---|
| **Titan Text Embeddings V2** | `amazon.titan-embed-text-v2:0` | 1024/512/256, float or binary | 8,192 tok | English-opt., 100+ langs | **Chosen.** KB default; configurable dims; binary for cost (OpenSearch only). |
| Titan Embeddings G1 Text | `amazon.titan-embed-text-v1` | 1536 (fixed) | 8,192 tok | 25+ langs | Legacy; prefer V2. |
| Cohere Embed English v3 | `cohere.embed-english-v3` | 1024 (fixed) | 512 tok | English only | Strong EN retrieval; short context → finer chunks. |
| Cohere Embed Multilingual v3 | `cohere.embed-multilingual-v3` | 1024 (fixed) | 512 tok | 100+ langs | Fallback if corpus heavily non-English. |
| (Multimodal) Titan Multimodal G1 / Nova Multimodal / Cohere v3 & v4 | — | 1024 | — | — | Text+image; check regional KB availability via `list-foundation-models`. |

**Vector stores:** OpenSearch Serverless (default/recommended; binary vectors; strong filtering) ·
S3 Vectors (cheapest; float-only) · Aurora pgvector · OpenSearch Managed · Neptune Analytics
(GraphRAG) · Pinecone / Redis / MongoDB Atlas (third-party). Quick-create: OpenSearch Serverless
and S3 Vectors.

Docs: `docs.aws.amazon.com/bedrock/latest/userguide/` — knowledge-base-supported.html,
titan-embedding-models.html, model-parameters-titan-embed-text.html,
model-parameters-embed-v3.html, knowledge-base-setup.html.
