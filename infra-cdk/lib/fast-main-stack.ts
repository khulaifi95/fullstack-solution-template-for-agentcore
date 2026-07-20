import * as cdk from "aws-cdk-lib"
import { Construct } from "constructs"
import { AppConfig } from "./utils/config-manager"

// Import constructs
import { BackendConstruct } from "./backend-construct"
import { AmplifyHostingConstruct } from "./amplify-hosting-construct"
import { CognitoConstruct } from "./cognito-construct"
import { KnowledgeBaseConstruct } from "./knowledge-base-construct"
import { StructuredDataConstruct } from "./structured-data-construct"

export interface FastAmplifyStackProps extends cdk.StackProps {
  config: AppConfig
}

export class FastMainStack extends cdk.Stack {
  public readonly amplifyHosting: AmplifyHostingConstruct
  public readonly backend: BackendConstruct
  public readonly cognito: CognitoConstruct
  public readonly knowledgeBase?: KnowledgeBaseConstruct
  public readonly structuredData?: StructuredDataConstruct

  constructor(scope: Construct, id: string, props: FastAmplifyStackProps) {
    const description =
      "Fullstack AgentCore Solution Template - Main Stack (v0.4.2) (uksb-v6dos0t5g8)"
    super(scope, id, { ...props, description })

    // Step 1: Create the Amplify construct to get the predictable domain
    this.amplifyHosting = new AmplifyHostingConstruct(this, `${id}-amplify`, {
      config: props.config,
    })

    this.cognito = new CognitoConstruct(this, `${id}-cognito`, {
      config: props.config,
      callbackUrls: ["http://localhost:3000", this.amplifyHosting.amplifyUrl],
    })

    // Step 2: Create backend construct with the predictable Amplify URL and Cognito details
    this.backend = new BackendConstruct(this, `${id}-backend`, {
      config: props.config,
      userPoolId: this.cognito.userPoolId,
      userPoolClientId: this.cognito.userPoolClientId,
      userPoolDomain: this.cognito.userPoolDomain,
      frontendUrl: this.amplifyHosting.amplifyUrl,
    })

    // Step 3: Optional unstructured retrieval lane (Bedrock KB + OpenSearch).
    // Created only when config.yaml defines a knowledge_base section.
    if (props.config.knowledge_base) {
      this.knowledgeBase = new KnowledgeBaseConstruct(this, `${id}-kb`, {
        config: props.config,
      })
    }

    // Step 4: Optional structured retrieval lane (Glue catalog + Athena).
    // Created only when config.yaml defines a structured_data section.
    if (props.config.structured_data) {
      this.structuredData = new StructuredDataConstruct(this, `${id}-structured`, {
        config: props.config,
      })
    }

    // Outputs
    new cdk.CfnOutput(this, "AmplifyAppId", {
      value: this.amplifyHosting.amplifyApp.appId,
      description: "Amplify App ID - use this for manual deployment",
      exportName: `${props.config.stack_name_base}-AmplifyAppId`,
    })

    new cdk.CfnOutput(this, "CognitoUserPoolId", {
      value: this.cognito.userPoolId,
      description: "Cognito User Pool ID",
      exportName: `${props.config.stack_name_base}-CognitoUserPoolId`,
    })

    new cdk.CfnOutput(this, "CognitoClientId", {
      value: this.cognito.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName: `${props.config.stack_name_base}-CognitoClientId`,
    })

    new cdk.CfnOutput(this, "CognitoDomain", {
      value: `${this.cognito.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: "Cognito Domain for OAuth",
      exportName: `${props.config.stack_name_base}-CognitoDomain`,
    })

    new cdk.CfnOutput(this, "RuntimeArn", {
      value: this.backend.runtimeArn,
      description: "AgentCore Runtime ARN",
      exportName: `${props.config.stack_name_base}-RuntimeArn`,
    })

    new cdk.CfnOutput(this, "MemoryArn", {
      value: this.backend.memoryArn,
      description: "AgentCore Memory ARN",
      exportName: `${props.config.stack_name_base}-MemoryArn`,
    })

    new cdk.CfnOutput(this, "FeedbackApiUrl", {
      value: this.backend.feedbackApiUrl,
      description: "Feedback API Gateway URL",
      exportName: `${props.config.stack_name_base}-FeedbackApiUrl`,
    })

    new cdk.CfnOutput(this, "AmplifyConsoleUrl", {
      value: `https://console.aws.amazon.com/amplify/apps/${this.amplifyHosting.amplifyApp.appId}`,
      description: "Amplify Console URL for monitoring deployments",
    })

    new cdk.CfnOutput(this, "AmplifyUrl", {
      value: this.amplifyHosting.amplifyUrl,
      description: "Amplify Frontend URL (available after deployment)",
    })

    new cdk.CfnOutput(this, "StagingBucketName", {
      value: this.amplifyHosting.stagingBucket.bucketName,
      description: "S3 bucket for Amplify deployment staging",
      exportName: `${props.config.stack_name_base}-StagingBucket`,
    })
  }
}
