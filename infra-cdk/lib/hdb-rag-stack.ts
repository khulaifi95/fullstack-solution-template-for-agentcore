import * as cdk from "aws-cdk-lib"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import { KnowledgeBaseConstruct } from "./knowledge-base-construct"
import { StructuredDataConstruct } from "./structured-data-construct"

export interface HdbRagStackProps extends cdk.StackProps {
  config: AppConfig
}

/**
 * Standalone stack for the HDB knowledge-management chatbot retrieval lanes:
 *   - unstructured: Bedrock KB + OpenSearch Serverless (KnowledgeBaseConstruct)
 *   - structured:   Glue Data Catalog + Athena          (StructuredDataConstruct)
 *
 * Deliberately SEPARATE from FastMainStack (the FAST baseline). The deployed
 * FAST-stack already ships its own KB (S3 Vectors) + DynamoDB structured lane;
 * this stack is an independent alternative strategy (OpenSearch + Athena text-
 * to-SQL) that runs alongside it without touching FAST-stack. Deploy explicitly
 * by stack name — it is not created as part of the FAST baseline deploy.
 */
export class HdbRagStack extends cdk.Stack {
  public readonly knowledgeBase?: KnowledgeBaseConstruct
  public readonly structuredData?: StructuredDataConstruct

  constructor(scope: Construct, id: string, props: HdbRagStackProps) {
    super(scope, id, {
      ...props,
      description: "HDB KM chatbot retrieval lanes (OpenSearch KB + Glue/Athena) — separate from FAST baseline",
    })

    if (props.config.knowledge_base) {
      this.knowledgeBase = new KnowledgeBaseConstruct(this, `${id}-kb`, {
        config: props.config,
      })
    }

    if (props.config.structured_data) {
      this.structuredData = new StructuredDataConstruct(this, `${id}-structured`, {
        config: props.config,
      })
    }
  }
}
