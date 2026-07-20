import * as cdk from "aws-cdk-lib"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as ssm from "aws-cdk-lib/aws-ssm"
import { Construct } from "constructs"
import { bedrock } from "@cdklabs/generative-ai-cdk-constructs"
import { AppConfig, KnowledgeBaseConfig } from "./utils/config-manager"

export interface KnowledgeBaseConstructProps {
  config: AppConfig
}

/**
 * Unstructured retrieval lane for the HDB knowledge-management chatbot.
 *
 * Provisions a Bedrock Knowledge Base backed by an OpenSearch Serverless vector
 * collection (created by the construct), fed from an S3 data source of PDFs
 * organized under per-space prefixes. Chunking, embedding, and FM-based advanced
 * parsing are driven by the `knowledge_base` section of config.yaml.
 *
 * The chunking strategy is IMMUTABLE once the data source exists — changing it
 * means creating a new data source / KB and re-ingesting (the "versioned
 * re-index + cutover" model in docs/HDB_KM_CHATBOT_ARCHITECTURE.md). The stack
 * logical id embeds the strategy so a strategy change forces replacement rather
 * than a failed in-place update.
 */
export class KnowledgeBaseConstruct extends Construct {
  public readonly knowledgeBaseId: string
  public readonly knowledgeBaseArn: string
  public readonly sourceBucket: s3.IBucket
  public readonly dataSourceId: string

  constructor(scope: Construct, id: string, props: KnowledgeBaseConstructProps) {
    super(scope, id)

    const kb = props.config.knowledge_base
    if (!kb) {
      throw new Error(
        "KnowledgeBaseConstruct requires a knowledge_base section in config.yaml"
      )
    }

    // Source bucket: reuse an existing one if named, otherwise create it and
    // export the name so scripts/ingest_hdb.py can stage documents into it.
    this.sourceBucket = kb.source_bucket_name
      ? s3.Bucket.fromBucketName(this, "SourceBucket", kb.source_bucket_name)
      : new s3.Bucket(this, "SourceBucket", {
          enforceSSL: true,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
          encryption: s3.BucketEncryption.S3_MANAGED,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        })

    const knowledgeBase = new bedrock.VectorKnowledgeBase(this, "KnowledgeBase", {
      embeddingsModel: this._embeddingModel(kb),
      vectorType: bedrock.VectorType.FLOATING_POINT,
      instruction:
        "Use this knowledge base to answer questions about HDB/HCSA policies, " +
        "SOPs, email correspondence, and financial/annual reports. Always cite " +
        "the source document and page.",
    })

    new bedrock.S3DataSource(this, "DocsDataSource", {
      knowledgeBase,
      bucket: this.sourceBucket,
      dataSourceName: `hdb-docs-${(kb.chunking_strategy || "HIERARCHICAL").toLowerCase()}`,
      chunkingStrategy: this._chunkingStrategy(kb),
      parsingStrategy: this._parsingStrategy(kb),
    })

    this.knowledgeBaseId = knowledgeBase.knowledgeBaseId
    this.knowledgeBaseArn = knowledgeBase.knowledgeBaseArn
    this.dataSourceId = "" // populated via CfnOutput from the L1 below if needed

    // Publish for the retrieve tool / Gateway and the ingest script.
    new ssm.StringParameter(this, "KbIdParam", {
      parameterName: `/${props.config.stack_name_base}/knowledge-base/id`,
      stringValue: this.knowledgeBaseId,
    })
    new ssm.StringParameter(this, "KbSourceBucketParam", {
      parameterName: `/${props.config.stack_name_base}/knowledge-base/source-bucket`,
      stringValue: this.sourceBucket.bucketName,
    })

    new cdk.CfnOutput(this, "KnowledgeBaseId", {
      value: this.knowledgeBaseId,
      description: "Bedrock Knowledge Base ID (unstructured retrieval lane)",
      exportName: `${props.config.stack_name_base}-KnowledgeBaseId`,
    })
    new cdk.CfnOutput(this, "KnowledgeBaseSourceBucket", {
      value: this.sourceBucket.bucketName,
      description: "S3 bucket for KB source documents — target for ingest_hdb.py --raw-bucket",
      exportName: `${props.config.stack_name_base}-KnowledgeBaseSourceBucket`,
    })
  }

  /** Map configured embedding model + dimensions to the library's model constant. */
  private _embeddingModel(kb: KnowledgeBaseConfig): bedrock.BedrockFoundationModel {
    const model = kb.embedding_model || "amazon.titan-embed-text-v2:0"
    if (model === "amazon.titan-embed-text-v2:0") {
      switch (kb.embedding_dimensions) {
        case 256:
          return bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_256
        case 512:
          return bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_512
        case 1024:
        default:
          return bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_1024
      }
    }
    if (model === "amazon.titan-embed-text-v1") {
      return bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V1
    }
    throw new Error(
      `Unsupported embedding_model '${model}'. Add a mapping in KnowledgeBaseConstruct._embeddingModel.`
    )
  }

  /** Build the chunking strategy from config. Immutable after data-source creation. */
  private _chunkingStrategy(kb: KnowledgeBaseConfig): bedrock.ChunkingStrategy {
    const maxTokens = kb.max_tokens ?? 300
    const overlap = kb.overlap_percentage ?? 20
    switch (kb.chunking_strategy) {
      case "FIXED_SIZE":
        return bedrock.ChunkingStrategy.fixedSize({
          maxTokens,
          overlapPercentage: overlap,
        })
      case "SEMANTIC":
        return bedrock.ChunkingStrategy.SEMANTIC
      case "NONE":
        return bedrock.ChunkingStrategy.NONE
      case "HIERARCHICAL":
      default:
        // Parent groups children for context; child chunks are what get embedded.
        return bedrock.ChunkingStrategy.hierarchical({
          overlapTokens: Math.round((overlap / 100) * maxTokens),
          maxParentTokenSize: Math.max(maxTokens * 4, 1500),
          maxChildTokenSize: maxTokens,
        })
    }
  }

  /**
   * FM-based advanced parsing so tables/figures in the financial reports survive
   * chunking (protects page-level citation fidelity). Returns undefined when
   * advanced parsing is disabled, falling back to the default Bedrock parser.
   */
  private _parsingStrategy(kb: KnowledgeBaseConfig): bedrock.ParsingStrategy | undefined {
    if (kb.advanced_parsing === false) {
      return undefined
    }
    const parsingModel = bedrock.CrossRegionInferenceProfile.fromConfig({
      geoRegion: bedrock.CrossRegionInferenceProfileRegion.US,
      model: bedrock.BedrockFoundationModel.ANTHROPIC_CLAUDE_4_SONNET_V1_0,
    })
    return bedrock.ParsingStrategy.foundationModel({ parsingModel })
  }
}
