import * as cdk from "aws-cdk-lib"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as iam from "aws-cdk-lib/aws-iam"
import * as glue from "aws-cdk-lib/aws-glue"
import * as athena from "aws-cdk-lib/aws-athena"
import * as ssm from "aws-cdk-lib/aws-ssm"
import { Construct } from "constructs"
import { AppConfig, StructuredDataConfig } from "./utils/config-manager"

export interface StructuredDataConstructProps {
  config: AppConfig
}

/**
 * Structured retrieval lane for the HDB knowledge-management chatbot.
 *
 * Catalogs the four Parquet datasets (contractors, development_projects, permits,
 * inspections) staged by scripts/ingest_hdb.py and makes them queryable via Athena
 * text-to-SQL. Provisions: a Glue database, a Glue crawler over the tables bucket,
 * an Athena workgroup + results bucket, and the IAM the `run_sql` tool needs.
 *
 * A crawler (rather than hand-declared tables) infers column types directly from
 * the Parquet footers, avoiding Athena type-mismatch errors. The crawler must be
 * run once after ingestion — its name is exported for that step.
 *
 * Join model (for the text-to-SQL prompt; column names differ from the FK target):
 *   development_projects.contractor_id -> contractors.contractor_id  (CONTR-*)
 *   permits.assignment_id              -> development_projects.project_id  (UP-*)
 *   inspections.case_id                -> development_projects.project_id  (UP-*)
 */
export class StructuredDataConstruct extends Construct {
  public readonly tablesBucket: s3.IBucket
  public readonly databaseName: string
  public readonly crawlerName: string
  public readonly workgroupName: string
  public readonly resultsBucket: s3.Bucket

  constructor(scope: Construct, id: string, props: StructuredDataConstructProps) {
    super(scope, id)

    const sd = props.config.structured_data
    if (!sd) {
      throw new Error(
        "StructuredDataConstruct requires a structured_data section in config.yaml"
      )
    }
    const stack = cdk.Stack.of(this)

    // Tables bucket: reuse if named, else create + export for ingest_hdb.py.
    this.tablesBucket = sd.tables_bucket_name
      ? s3.Bucket.fromBucketName(this, "TablesBucket", sd.tables_bucket_name)
      : new s3.Bucket(this, "TablesBucket", {
          enforceSSL: true,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
          encryption: s3.BucketEncryption.S3_MANAGED,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        })

    // Athena query results bucket (separate lifecycle; results expire).
    this.resultsBucket = new s3.Bucket(this, "AthenaResults", {
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(14) }],
    })

    // Glue database.
    this.databaseName = sd.database_name || "hdb_structured"
    const database = new glue.CfnDatabase(this, "Database", {
      catalogId: stack.account,
      databaseInput: {
        name: this.databaseName,
        description: "HDB structured datasets (contractors, projects, permits, inspections)",
      },
    })

    // IAM role for the crawler.
    const crawlerRole = new iam.Role(this, "CrawlerRole", {
      assumedBy: new iam.ServicePrincipal("glue.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSGlueServiceRole"),
      ],
    })
    this.tablesBucket.grantRead(crawlerRole)

    const crawler = new glue.CfnCrawler(this, "Crawler", {
      role: crawlerRole.roleArn,
      databaseName: this.databaseName,
      targets: {
        s3Targets: [{ path: `s3://${this.tablesBucket.bucketName}/` }],
      },
      // Keep table names stable and one table per top-level prefix.
      schemaChangePolicy: {
        updateBehavior: "UPDATE_IN_DATABASE",
        deleteBehavior: "LOG",
      },
      configuration: JSON.stringify({
        Version: 1.0,
        Grouping: { TableLevelConfiguration: 2 },
      }),
    })
    crawler.addDependency(database)
    this.crawlerName = crawler.ref

    // Athena workgroup with results location enforced.
    this.workgroupName = sd.workgroup_name || `${props.config.stack_name_base}-hdb`
    new athena.CfnWorkGroup(this, "Workgroup", {
      name: this.workgroupName,
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        resultConfiguration: {
          outputLocation: `s3://${this.resultsBucket.bucketName}/results/`,
        },
      },
    })

    // Publish for the run_sql tool / Gateway and the crawler-run step.
    const params: Record<string, string> = {
      "structured/database": this.databaseName,
      "structured/workgroup": this.workgroupName,
      "structured/crawler": this.crawlerName,
      "structured/tables-bucket": this.tablesBucket.bucketName,
      "structured/results-bucket": this.resultsBucket.bucketName,
    }
    for (const [suffix, value] of Object.entries(params)) {
      new ssm.StringParameter(this, `Param-${suffix.split("/")[1]}`, {
        parameterName: `/${props.config.stack_name_base}/${suffix}`,
        stringValue: value,
      })
    }

    new cdk.CfnOutput(this, "StructuredTablesBucket", {
      value: this.tablesBucket.bucketName,
      description: "S3 bucket for Parquet tables — target for ingest_hdb.py --tables-bucket",
      exportName: `${props.config.stack_name_base}-StructuredTablesBucket`,
    })
    new cdk.CfnOutput(this, "GlueCrawlerName", {
      value: this.crawlerName,
      description: "Run this crawler once after ingestion: aws glue start-crawler --name <value>",
    })
    new cdk.CfnOutput(this, "AthenaWorkgroup", {
      value: this.workgroupName,
      description: "Athena workgroup for the run_sql tool",
      exportName: `${props.config.stack_name_base}-AthenaWorkgroup`,
    })
    new cdk.CfnOutput(this, "GlueDatabaseName", {
      value: this.databaseName,
      description: "Glue database holding the structured tables",
      exportName: `${props.config.stack_name_base}-GlueDatabaseName`,
    })
  }
}
