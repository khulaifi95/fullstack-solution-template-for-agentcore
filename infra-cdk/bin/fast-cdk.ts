#!/usr/bin/env node
import * as cdk from "aws-cdk-lib"
import { FastMainStack } from "../lib/fast-main-stack"
import { HdbRagStack } from "../lib/hdb-rag-stack"
import { ConfigManager } from "../lib/utils/config-manager"

// Load configuration using ConfigManager
const configManager = new ConfigManager("config.yaml")

// Initial props consist of configuration parameters
const props = configManager.getProps()

const app = new cdk.App()

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
}

// Safety flag: when `-c hdbRagOnly=true`, DO NOT instantiate FastMainStack.
// This worktree's baseline code predates the KB/DynamoDB work currently deployed
// in FAST-stack, so synthesizing FastMainStack here would produce a template
// missing those resources — a `cdk deploy` would then DELETE the live KB and
// DynamoDB. Skipping it makes it impossible to touch FAST-stack, and also avoids
// the Docker-based Lambda bundling FastMainStack requires.
const hdbRagOnly = app.node.tryGetContext("hdbRagOnly") === "true"

if (!hdbRagOnly) {
  // FAST baseline stack (unchanged — does NOT include the HDB retrieval lanes).
  new FastMainStack(app, props.stack_name_base, {
    config: props,
    env,
  })
}

// HDB retrieval lanes (OpenSearch KB + Glue/Athena) — a SEPARATE stack that runs
// alongside FAST without modifying it. Deploy explicitly:
//   cdk deploy <stack_name_base>-hdb-rag
if (props.knowledge_base || props.structured_data) {
  new HdbRagStack(app, `${props.stack_name_base}-hdb-rag`, {
    config: props,
    env,
  })
}

app.synth()
