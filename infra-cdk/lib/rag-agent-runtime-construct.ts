import * as cdk from "aws-cdk-lib"
import * as iam from "aws-cdk-lib/aws-iam"
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore"
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets"
import { Construct } from "constructs"
import * as path from "path"
import { AppConfig } from "./utils/config-manager"
import { AgentCoreRole } from "./utils/agentcore-role"

export interface RagAgentRuntimeConstructProps {
  config: AppConfig
  /**
   * The EXISTING FAST stack name whose shared resources this runtime reuses:
   * the Gateway URL SSM parameter (/<baseStack>/gateway_url) and the OAuth2
   * credential provider (<baseStack>-runtime-gateway-auth). Defaults to the
   * config's stack_name_base.
   */
  baseStackName: string
  /** Cognito user pool id for the runtime's JWT authorizer (FAST-stack's pool). */
  userPoolId: string
  /** Cognito app client id allowed to invoke the runtime (FAST-stack's client). */
  userPoolClientId: string
  /** KB arn so the agent's execution role may call bedrock:Retrieve directly if needed. */
  knowledgeBaseArn?: string
}

/**
 * A SECOND AgentCore Runtime (separate from FAST-stack's) running the HDB RAG
 * orchestrator agent (patterns/strands-single-agent with the RAG system prompt).
 *
 * It deliberately REUSES FAST-stack's shared plumbing by referencing it by name:
 * STACK_NAME is set to the base FAST stack, so the agent reads the same
 * /<baseStack>/gateway_url SSM parameter and uses the same OAuth2 credential
 * provider to authenticate to the shared Gateway (where hdb-retrieve / hdb-run_sql
 * are registered). This runtime has its own execution role + Memory so it never
 * modifies FAST-stack.
 *
 * Uses the Docker artifact path (the pattern's Dockerfile); the CodeBuild deploy
 * provides Docker.
 */
export class RagAgentRuntimeConstruct extends Construct {
  public readonly runtimeArn: string
  public readonly runtimeId: string

  constructor(scope: Construct, id: string, props: RagAgentRuntimeConstructProps) {
    super(scope, id)

    const stack = cdk.Stack.of(this)
    const pattern = props.config.backend?.pattern || "strands-single-agent"
    const base = props.baseStackName

    // Build the agent image from the repo root using the pattern's Dockerfile.
    const artifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.resolve(__dirname, "..", ".."), // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
      {
        platform: ecr_assets.Platform.LINUX_ARM64,
        file: `patterns/${pattern}/Dockerfile`,
      }
    )

    // Execution role: base AgentCore permissions + the extras the agent needs.
    const agentRole = new AgentCoreRole(this, "RagAgentRole")

    // Own Memory resource (short-term; semantic strategy defined but only used
    // when USE_LONG_TERM_MEMORY=true).
    const memory = new agentcore.Memory(this, "RagAgentMemory", {
      memoryName: cdk.Names.uniqueResourceName(this, { maxLength: 48 }),
      expirationDuration: cdk.Duration.days(30),
      description: `Short-term memory for ${base} HDB RAG agent`,
      memoryStrategies: [
        agentcore.MemoryStrategy.usingSemantic({
          strategyName: "FactExtractor",
          namespaces: ["/facts/{actorId}"],
        }),
      ],
      executionRole: agentRole,
    })

    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "MemoryResourceAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:RetrieveMemoryRecords",
        ],
        resources: [memory.memoryArn],
      })
    )

    // Read the SHARED gateway url + any config under the base FAST stack namespace.
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SSMParameterAccess",
        effect: iam.Effect.ALLOW,
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [`arn:aws:ssm:${stack.region}:${stack.account}:parameter/${base}/*`],
      })
    )

    // Code Interpreter (the agent still wires the tool).
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CodeInterpreterAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:StartCodeInterpreterSession",
          "bedrock-agentcore:StopCodeInterpreterSession",
          "bedrock-agentcore:InvokeCodeInterpreter",
        ],
        resources: [`arn:aws:bedrock-agentcore:${stack.region}:aws:code-interpreter/*`],
      })
    )

    // OAuth2 credential provider (shared, owned by FAST-stack) so the agent can
    // mint an M2M token to call the Gateway.
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "OAuth2CredentialProviderAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:GetOauth2CredentialProvider",
          "bedrock-agentcore:GetResourceOauth2Token",
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        ],
        resources: ["*"],
      })
    )

    // Cognito call the gateway.py Approach 1 makes to mint the user-identity M2M token.
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CognitoInitiateAuth",
        effect: iam.Effect.ALLOW,
        actions: ["cognito-idp:DescribeUserPoolClient", "cognito-idp:InitiateAuth"],
        resources: [
          `arn:aws:cognito-idp:${stack.region}:${stack.account}:userpool/${props.userPoolId}`,
        ],
      })
    )

    if (props.knowledgeBaseArn) {
      agentRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "KnowledgeBaseRetrieve",
          effect: iam.Effect.ALLOW,
          actions: ["bedrock:Retrieve"],
          resources: [props.knowledgeBaseArn],
        })
      )
    }

    // Read the machine-client secret (for the direct-Cognito M2M token call).
    // The secret is owned by FAST-stack; grant read by ARN pattern.
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "MachineClientSecretRead",
        effect: iam.Effect.ALLOW,
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${stack.region}:${stack.account}:secret:${base}*`,
        ],
      })
    )

    const authorizerConfiguration = agentcore.RuntimeAuthorizerConfiguration.usingJWT(
      `https://cognito-idp.${stack.region}.amazonaws.com/${props.userPoolId}/.well-known/openid-configuration`,
      [props.userPoolClientId]
    )

    const runtime = new agentcore.Runtime(this, "RagRuntime", {
      runtimeName: `${base.replace(/-/g, "_")}_HdbRagAgent`,
      agentRuntimeArtifact: artifact,
      executionRole: agentRole,
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      authorizerConfiguration,
      requestHeaderConfiguration: { allowlistedHeaders: ["Authorization"] },
      // STACK_NAME points at the BASE FAST stack so the agent reuses its
      // gateway_url SSM param and OAuth2 credential provider.
      environmentVariables: {
        AWS_REGION: stack.region,
        AWS_DEFAULT_REGION: stack.region,
        MEMORY_ID: memory.memoryId,
        STACK_NAME: base,
        GATEWAY_CREDENTIAL_PROVIDER_NAME: `${base}-runtime-gateway-auth`,
        USE_LONG_TERM_MEMORY: props.config.backend.use_long_term_memory ? "true" : "false",
        LTM_TOP_K: String(props.config.backend.ltm_top_k),
        LTM_RELEVANCE_SCORE: String(props.config.backend.ltm_relevance_score),
      },
      description: `HDB RAG orchestrator runtime (reuses ${base} gateway)`,
    })

    this.runtimeArn = runtime.agentRuntimeArn
    this.runtimeId = runtime.agentRuntimeId

    new cdk.CfnOutput(this, "RagRuntimeArn", {
      value: this.runtimeArn,
      description: "HDB RAG orchestrator AgentCore Runtime ARN (separate from FAST runtime)",
    })
    new cdk.CfnOutput(this, "RagRuntimeId", {
      value: this.runtimeId,
      description: "HDB RAG orchestrator AgentCore Runtime id",
    })
  }
}
