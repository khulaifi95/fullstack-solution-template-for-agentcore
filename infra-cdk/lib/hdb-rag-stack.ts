import * as cdk from "aws-cdk-lib"
import * as fs from "fs"
import * as path from "path"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as logs from "aws-cdk-lib/aws-logs"
import * as iam from "aws-cdk-lib/aws-iam"
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import { KnowledgeBaseConstruct } from "./knowledge-base-construct"
import { StructuredDataConstruct } from "./structured-data-construct"
import { RagAgentRuntimeConstruct } from "./rag-agent-runtime-construct"

export interface HdbRagStackProps extends cdk.StackProps {
  config: AppConfig
  /**
   * Id of the EXISTING AgentCore Gateway (in FAST-stack) to attach the
   * retrieve / run_sql tool targets to. When omitted, the tools' Lambdas are
   * still created but not registered as gateway targets. Pass via
   * `-c gatewayId=<id>` or the config.
   */
  gatewayId?: string
  /**
   * ARN of the existing gateway's execution role (owned by FAST-stack). Required
   * for the gateway targets to validate — AgentCore checks that this role can
   * invoke the tool Lambdas. Passed via `-c gatewayRoleArn=<arn>`.
   */
  gatewayRoleArn?: string
  /**
   * When set, also stand up a SEPARATE AgentCore Runtime running the HDB RAG
   * orchestrator agent, reusing the base FAST stack's shared gateway/OAuth.
   * Requires the Cognito values below. Enabled via `-c deployRagRuntime=true`.
   */
  deployRagRuntime?: boolean
  /** Base FAST stack name whose shared gateway/OAuth the runtime reuses. */
  baseStackName?: string
  /** Cognito user pool id (FAST-stack) for the runtime JWT authorizer. */
  userPoolId?: string
  /** Cognito app client id (FAST-stack) allowed to invoke the runtime. */
  userPoolClientId?: string
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
  private _gatewayRole?: iam.IRole

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

    // Register the retrieve / run_sql tools as targets on the EXISTING FAST-stack
    // gateway (referenced by id — we never redeploy FastMainStack). Skipped when
    // no gatewayId is supplied.
    if (props.gatewayId) {
      this._addGatewayTools(props.gatewayId, props.gatewayRoleArn)
    }

    // Optional: a separate AgentCore Runtime running the RAG orchestrator agent.
    if (props.deployRagRuntime) {
      if (!props.userPoolId || !props.userPoolClientId) {
        throw new Error(
          "deployRagRuntime requires userPoolId and userPoolClientId (pass -c userPoolId=... -c userPoolClientId=...)"
        )
      }
      new RagAgentRuntimeConstruct(this, `${id}-runtime`, {
        config: props.config,
        baseStackName: props.baseStackName || props.config.stack_name_base,
        userPoolId: props.userPoolId,
        userPoolClientId: props.userPoolClientId,
        knowledgeBaseArn: this.knowledgeBase?.knowledgeBaseArn,
      })
    }
  }

  /** Create the two tool Lambdas and attach them to the existing gateway. */
  private _addGatewayTools(gatewayId: string, gatewayRoleArn?: string): void {
    const toolsRoot = path.join(__dirname, "..", "..", "gateway", "tools")

    // Import the existing gateway execution role (owned by FAST-stack) so we can
    // grant it invoke permission on our tool Lambdas. mutable:true lets CDK add a
    // policy to the role FROM THIS STACK (a separate policy resource, removed when
    // this stack is destroyed) — it does not redeploy or rewrite FastMainStack.
    this._gatewayRole = gatewayRoleArn
      ? iam.Role.fromRoleArn(this, "ImportedGatewayRole", gatewayRoleArn, { mutable: true })
      : undefined

    if (this.knowledgeBase) {
      const retrieveFn = new lambda.Function(this, "RetrieveToolLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "retrieve_lambda.handler",
        code: lambda.Code.fromAsset(path.join(toolsRoot, "retrieve")), // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
        timeout: cdk.Duration.seconds(30),
        environment: { KNOWLEDGE_BASE_ID: this.knowledgeBase.knowledgeBaseId },
        logGroup: new logs.LogGroup(this, "RetrieveToolLogGroup", {
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })
      retrieveFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["bedrock:Retrieve"],
          resources: [this.knowledgeBase.knowledgeBaseArn],
        })
      )
      this._attachTarget(gatewayId, "hdb-retrieve", retrieveFn, path.join(toolsRoot, "retrieve", "tool_spec.json"))
    }

    if (this.structuredData) {
      const sd = this.structuredData
      const sqlFn = new lambda.Function(this, "RunSqlToolLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "run_sql_lambda.handler",
        code: lambda.Code.fromAsset(path.join(toolsRoot, "run_sql")), // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
        timeout: cdk.Duration.seconds(30),
        environment: {
          ATHENA_DATABASE: sd.databaseName,
          ATHENA_WORKGROUP: sd.workgroupName,
        },
        logGroup: new logs.LogGroup(this, "RunSqlToolLogGroup", {
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })
      // Athena + Glue read + result-bucket write for query execution.
      sqlFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:StopQueryExecution",
            "glue:GetTable",
            "glue:GetTables",
            "glue:GetDatabase",
            "glue:GetPartitions",
          ],
          resources: ["*"],
        })
      )
      sd.tablesBucket.grantRead(sqlFn)
      sd.resultsBucket.grantReadWrite(sqlFn)
      this._attachTarget(gatewayId, "hdb-run-sql", sqlFn, path.join(toolsRoot, "run_sql", "tool_spec.json"))
    }
  }

  /**
   * Attach one Lambda tool as an MCP target on the existing gateway, using the
   * tool_spec.json as the inline tool schema. Grants the gateway service
   * principal permission to invoke the Lambda.
   */
  private _attachTarget(
    gatewayId: string,
    name: string,
    fn: lambda.Function,
    specPath: string
  ): void {
    const spec = JSON.parse(fs.readFileSync(specPath, "utf-8")) as Array<{
      name: string
      description: string
      inputSchema: unknown
    }>

    // Resource-based grant to the service principal (defence in depth)...
    fn.addPermission(`${name}-gw-invoke`, {
      principal: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      action: "lambda:InvokeFunction",
    })
    // ...and the identity-policy grant AgentCore actually validates: the gateway
    // EXECUTION ROLE must be allowed to invoke this Lambda.
    if (this._gatewayRole) {
      fn.grantInvoke(this._gatewayRole)
    }

    new agentcore.CfnGatewayTarget(this, `${name}-target`, {
      gatewayIdentifier: gatewayId,
      name,
      description: `HDB ${name} tool`,
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: fn.functionArn,
            toolSchema: {
              inlinePayload: spec.map((t) => ({
                name: t.name,
                description: t.description,
                inputSchema: t.inputSchema as agentcore.CfnGatewayTarget.SchemaDefinitionProperty,
              })),
            },
          },
        },
      },
      credentialProviderConfigurations: [
        { credentialProviderType: "GATEWAY_IAM_ROLE" },
      ],
    })
  }
}
