import * as cdk from "aws-cdk-lib"
import * as iam from "aws-cdk-lib/aws-iam"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment"
import * as s3vectors from "aws-cdk-lib/aws-s3vectors"
import * as bedrock from "aws-cdk-lib/aws-bedrock"
import * as dynamodb from "aws-cdk-lib/aws-dynamodb"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as logs from "aws-cdk-lib/aws-logs"
import * as cr from "aws-cdk-lib/custom-resources"
import { Construct } from "constructs"
import * as path from "path"

/**
 * Provisions a Bedrock Knowledge Base (RAG) plus a structured-data DynamoDB
 * table, both seeded from the repo's mock dataset at DEPLOY time.
 *
 * Data flow (all happens during `cdk deploy`, no runtime upload):
 *   mock-data/            --BucketDeployment-->  s3://sourceBucket/
 *     knowledge/*.pdf     --Bedrock ingestion-->  S3 Vectors index (RAG)
 *     structured/*.json   --ddb-seed Lambda---->  DynamoDB table (lookup)
 *
 * The Knowledge Base uses S3 Vectors (cheapest managed vector store) with
 * Titan Text Embeddings v2 (1024-dim). The data source enables BEDROCK_
 * FOUNDATION_MODEL advanced parsing so scanned/image PDFs are read by a
 * multimodal model — no separate OCR step. FM parsing falls back to the
 * default parser per-file on failure, so it can never fail ingestion.
 */
export interface KnowledgeBaseConstructProps {
  /** stack_name_base from config.yaml, used for resource naming. */
  stackNameBase: string
  /** Absolute path to the repo root (for locating mock-data/). */
  repoRoot: string
}

export class KnowledgeBaseConstruct extends Construct {
  public readonly knowledgeBaseId: string
  public readonly structuredTable: dynamodb.Table
  public readonly sourceBucket: s3.Bucket

  constructor(scope: Construct, id: string, props: KnowledgeBaseConstructProps) {
    super(scope, id)

    const stack = cdk.Stack.of(this)
    const region = stack.region
    const account = stack.account
    const { stackNameBase } = props

    // ---- Source bucket + deploy the mock dataset into it ------------------
    const sourceBucket = new s3.Bucket(this, "KbSourceBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    })
    this.sourceBucket = sourceBucket

    new s3deploy.BucketDeployment(this, "KbSourceDeployment", {
      sources: [s3deploy.Source.asset(path.join(props.repoRoot, "mock-data"))],
      destinationBucket: sourceBucket,
      // large corpus; give the bundler room
      memoryLimit: 1024,
    })

    // Multimodal FM parsing extracts images/tables from PDFs and stores them
    // here; the KB requires a supplemental S3 location for this.
    const supplementalBucket = new s3.Bucket(this, "KbSupplementalBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    })

    // ---- S3 Vectors: bucket + index --------------------------------------
    // Titan Text Embeddings v2 emits 1024-dim float32 vectors; cosine distance.
    const vectorBucket = new s3vectors.CfnVectorBucket(this, "VectorBucket", {
      vectorBucketName: `${stackNameBase.toLowerCase().replace(/[^a-z0-9-]/g, "-")}-kb-vectors`.slice(0, 63),
    })

    const vectorIndex = new s3vectors.CfnIndex(this, "VectorIndex", {
      vectorBucketName: vectorBucket.vectorBucketName!,
      indexName: "kb-index",
      dataType: "float32",
      dimension: 1024,
      distanceMetric: "cosine",
      // Bedrock stores chunk text + metadata as non-filterable index metadata.
      metadataConfiguration: {
        nonFilterableMetadataKeys: ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"],
      },
    })
    vectorIndex.addDependency(vectorBucket)

    // ---- IAM role assumed by the Bedrock Knowledge Base service ----------
    const embeddingModelArn = `arn:aws:bedrock:${region}::foundation-model/amazon.titan-embed-text-v2:0`
    // Multimodal FM used for advanced (image/scanned) PDF parsing.
    const parsingModelArn = `arn:aws:bedrock:${region}:${account}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0`

    const kbRole = new iam.Role(this, "KnowledgeBaseRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
      description: `Bedrock Knowledge Base service role for ${stackNameBase}`,
    })

    kbRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeModels",
        actions: ["bedrock:InvokeModel", "bedrock:GetInferenceProfile"],
        resources: [
          embeddingModelArn,
          parsingModelArn,
          // inference profiles resolve to region-specific foundation models
          `arn:aws:bedrock:*::foundation-model/anthropic.claude-*`,
          `arn:aws:bedrock:${region}:${account}:inference-profile/*`,
        ],
      })
    )
    sourceBucket.grantRead(kbRole)
    supplementalBucket.grantReadWrite(kbRole)
    kbRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "S3VectorsAccess",
        actions: ["s3vectors:*"],
        resources: [vectorBucket.attrVectorBucketArn, vectorIndex.attrIndexArn],
      })
    )

    // ---- Knowledge Base + Data Source ------------------------------------
    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, "KnowledgeBase", {
      name: `${stackNameBase.replace(/[^a-zA-Z0-9-]/g, "-")}-kb`.slice(0, 63),
      roleArn: kbRole.roleArn,
      knowledgeBaseConfiguration: {
        type: "VECTOR",
        vectorKnowledgeBaseConfiguration: {
          embeddingModelArn,
          // Required for multimodal FM parsing — stores extracted images/tables.
          supplementalDataStorageConfiguration: {
            supplementalDataStorageLocations: [
              {
                supplementalDataStorageLocationType: "S3",
                s3Location: { uri: `s3://${supplementalBucket.bucketName}` },
              },
            ],
          },
        },
      },
      storageConfiguration: {
        type: "S3_VECTORS",
        s3VectorsConfiguration: {
          vectorBucketArn: vectorBucket.attrVectorBucketArn,
          indexArn: vectorIndex.attrIndexArn,
        },
      },
    })
    knowledgeBase.node.addDependency(vectorIndex)
    knowledgeBase.node.addDependency(kbRole)

    const dataSource = new bedrock.CfnDataSource(this, "KbDataSource", {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: "mock-dataset-pdfs",
      dataSourceConfiguration: {
        type: "S3",
        s3Configuration: {
          bucketArn: sourceBucket.bucketArn,
          // Only ingest the PDF corpus; structured JSON goes to DynamoDB.
          inclusionPrefixes: ["knowledge/"],
        },
      },
      vectorIngestionConfiguration: {
        // Multimodal FM parsing reads scanned/image PDFs (no OCR step).
        // Falls back to the default parser per-file on failure.
        parsingConfiguration: {
          parsingStrategy: "BEDROCK_FOUNDATION_MODEL",
          bedrockFoundationModelConfiguration: {
            modelArn: parsingModelArn,
            parsingModality: "MULTIMODAL",
          },
        },
      },
    })
    dataSource.addDependency(knowledgeBase)

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId

    // ---- Deploy-time ingestion via custom resource -----------------------
    const ingestLambda = new lambda.Function(this, "KbIngestLambda", {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(props.repoRoot, "infra-cdk", "lambdas", "kb-ingest")),
      timeout: cdk.Duration.minutes(15),
      logGroup: new logs.LogGroup(this, "KbIngestLambdaLogGroup", {
        logGroupName: `/aws/lambda/${stackNameBase}-kb-ingest`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })
    ingestLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"],
        resources: [knowledgeBase.attrKnowledgeBaseArn],
      })
    )

    const ingestProvider = new cr.Provider(this, "KbIngestProvider", {
      onEventHandler: ingestLambda,
    })
    const ingestResource = new cdk.CustomResource(this, "KbIngestResource", {
      serviceToken: ingestProvider.serviceToken,
      properties: {
        KnowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
        DataSourceId: dataSource.attrDataSourceId,
        // Re-run ingestion whenever the deployment changes.
        Trigger: new Date().toISOString(),
      },
    })
    ingestResource.node.addDependency(dataSource)

    // ---- Structured data: DynamoDB table + deploy-time seeding -----------
    // PK = pk (entity type + id), so all four datasets share one table and
    // the agent can query by Project ID / Contractor ID across them.
    this.structuredTable = new dynamodb.Table(this, "StructuredTable", {
      tableName: `${stackNameBase}-structured`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    })

    const seedLambda = new lambda.Function(this, "DdbSeedLambda", {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(props.repoRoot, "infra-cdk", "lambdas", "ddb-seed")),
      timeout: cdk.Duration.minutes(10),
      memorySize: 512,
      logGroup: new logs.LogGroup(this, "DdbSeedLambdaLogGroup", {
        logGroupName: `/aws/lambda/${stackNameBase}-ddb-seed`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })
    sourceBucket.grantRead(seedLambda)
    this.structuredTable.grantWriteData(seedLambda)

    const seedProvider = new cr.Provider(this, "DdbSeedProvider", {
      onEventHandler: seedLambda,
    })
    const seedResource = new cdk.CustomResource(this, "DdbSeedResource", {
      serviceToken: seedProvider.serviceToken,
      properties: {
        Bucket: sourceBucket.bucketName,
        Prefix: "structured/",
        TableName: this.structuredTable.tableName,
        Trigger: new Date().toISOString(),
      },
    })
    seedResource.node.addDependency(this.structuredTable)

    // ---- Outputs ----------------------------------------------------------
    new cdk.CfnOutput(this, "KnowledgeBaseIdOutput", {
      description: "Bedrock Knowledge Base ID for the mock dataset",
      value: knowledgeBase.attrKnowledgeBaseId,
    })
    new cdk.CfnOutput(this, "StructuredTableOutput", {
      description: "DynamoDB table holding the structured mock datasets",
      value: this.structuredTable.tableName,
    })
  }
}
