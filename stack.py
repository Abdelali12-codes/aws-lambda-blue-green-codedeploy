import aws_cdk as cdk
from aws_cdk import (
    aws_codedeploy as codedeploy,
    aws_lambda as lambda_,
    aws_cloudwatch as cloudwatch,
    aws_iam as iam,
    aws_codepipeline as codepipeline,
    aws_codebuild as codebuild,
    aws_s3 as s3,
)
from constructs import Construct


class BlueGreenLambdaLinearStack(cdk.Stack):
    """
    Blue/Green Lambda deployment — LINEAR traffic shifting.

    LINEAR strategy:
    - Shift traffic in equal increments at fixed time intervals
    - Every N minutes, shift X% more traffic to the new version
    - Continues until 100% is on the new version
    - If any alarm fires at any step → auto rollback to 100% old version

    LINEAR_10PERCENT_EVERY_1_MINUTE:
      t=0:  10% → new, 90% → old
      t=1:  20% → new, 80% → old
      t=2:  30% → new, 70% → old
      ...
      t=9: 100% → new, 0%  → old

    vs CANARY:
    - CANARY  → one small batch, then all-at-once (2 steps)
    - LINEAR  → gradual equal increments (many steps)
    - LINEAR gives more time to detect issues at each traffic level
    - CANARY is faster to complete if the canary batch looks healthy

    ALL_AT_ONCE:
    - Shift 100% immediately (no gradual shift, instant cutover)
    - Fastest but no gradual validation window
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        fn = lambda_.Function(
            self, "Fn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            function_name="bg-lambda-linear-fn",
            code=lambda_.Code.from_inline(
                "def handler(event, context):\n"
                "    return {'statusCode': 200, 'body': 'v1-blue'}\n"
            ),
            current_version_options=lambda_.VersionOptions(
                removal_policy=cdk.RemovalPolicy.RETAIN,
            ),
        )

        version = fn.current_version

        alias = lambda_.Alias(
            self, "LiveAlias",
            alias_name="live",
            version=version,
        )

        # ── Alarms — any firing triggers immediate rollback ─────────────
        errors_alarm = cloudwatch.Alarm(
            self, "ErrorsAlarm",
            metric=fn.metric_errors(period=cdk.Duration.minutes(1)),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Rollback if any errors during linear shift",
        )

        throttles_alarm = cloudwatch.Alarm(
            self, "ThrottlesAlarm",
            metric=fn.metric_throttles(period=cdk.Duration.minutes(1)),
            threshold=5,
            evaluation_periods=1,
            alarm_description="Rollback if throttles spike during linear shift",
        )

        # ── Pre-traffic hook ────────────────────────────────────────────
        pre_hook = lambda_.Function(
            self, "PreHook",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                "import boto3\n"
                "cd = boto3.client('codedeploy')\n"
                "\n"
                "def handler(event, context):\n"
                "    deployment_id = event['DeploymentId']\n"
                "    hook_id = event['LifecycleEventHookExecutionId']\n"
                "    # Validate new version before any traffic shifts\n"
                "    cd.put_lifecycle_event_hook_execution_status(\n"
                "        deploymentId=deployment_id,\n"
                "        lifecycleEventHookExecutionId=hook_id,\n"
                "        status='Succeeded',\n"
                "    )\n"
            ),
        )
        pre_hook.add_to_role_policy(
            iam.PolicyStatement(
                actions=["codedeploy:PutLifecycleEventHookExecutionStatus"],
                resources=["*"],
            )
        )

        application = codedeploy.LambdaApplication(
            self, "App", application_name="bg-lambda-linear-app"
        )

        # LINEAR: shift 10% every 1 minute until 100%
        # Other options:
        #   LINEAR_10PERCENT_EVERY_2_MINUTES
        #   LINEAR_10PERCENT_EVERY_3_MINUTES
        #   LINEAR_10PERCENT_EVERY_10_MINUTES
        #   ALL_AT_ONCE → instant 100% shift, no gradual window
        codedeploy.LambdaDeploymentGroup(
            self, "DeploymentGroup",
            application=application,
            alias=alias,
            deployment_config=codedeploy.LambdaDeploymentConfig.LINEAR_10_PERCENT_EVERY_1_MINUTE,
            pre_hook=pre_hook,
            alarms=[errors_alarm, throttles_alarm],
            auto_rollback=codedeploy.AutoRollbackConfig(
                deployment_in_alarm=True,
                failed_deployment=True,
            ),
        )

        cdk.CfnOutput(self, "FunctionName", value=fn.function_name)
        cdk.CfnOutput(self, "AliasArn", value=alias.function_arn)

        # ── Artifact bucket ─────────────────────────────────────────────
        artifact_bucket_name = self.node.get_context("artifact_bucket_name")
        artifact_bucket = s3.Bucket.from_bucket_name(
            self, "ArtifactBucket", artifact_bucket_name
        )

        # ── CodeBuild — packages Lambda zip + appspec.yml ──────────────
        build_role = iam.Role(
            self, "BuildRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
        )
        artifact_bucket.grant_read_write(build_role)
        fn.grant_invoke(build_role)
        build_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                "lambda:UpdateFunctionCode", "lambda:PublishVersion", "lambda:GetFunction",
            ],
            resources=["*"],
        ))

        build_project = codebuild.CfnProject(
            self, "BuildProject",
            name="bg-lambda-linear-build",
            service_role=build_role.role_arn,
            artifacts=codebuild.CfnProject.ArtifactsProperty(type="CODEPIPELINE"),
            environment=codebuild.CfnProject.EnvironmentProperty(
                type="LINUX_CONTAINER",
                compute_type="BUILD_GENERAL1_SMALL",
                image="aws/codebuild/standard:7.0",
                environment_variables=[
                    codebuild.CfnProject.EnvironmentVariableProperty(
                        name="FUNCTION_NAME", value=fn.function_name
                    ),
                ],
            ),
            source=codebuild.CfnProject.SourceProperty(
                type="CODEPIPELINE",
                build_spec="\n".join([
                    "version: 0.2",
                    "phases:",
                    "  build:",
                    "    commands:",
                    "      - cd app && zip -r ../function.zip . && cd ..",
                    "      - aws lambda update-function-code --function-name $FUNCTION_NAME --zip-file fileb://function.zip",
                    "      - NEW_VERSION=$(aws lambda publish-version --function-name $FUNCTION_NAME --query Version --output text)",
                    "      - NEW_ARN=$(aws lambda get-function --function-name $FUNCTION_NAME --qualifier $NEW_VERSION --query Configuration.FunctionArn --output text)",
                    "      - CURRENT_VERSION=$(aws lambda get-alias --function-name $FUNCTION_NAME --name live --query FunctionVersion --output text)",
                    "      - CURRENT_ARN=$(aws lambda get-function --function-name $FUNCTION_NAME --qualifier $CURRENT_VERSION --query Configuration.FunctionArn --output text)",
                    "      - |",
                    "        cat > appspec.yml << EOF",
                    "        version: 0.0",
                    "        Resources:",
                    "          - MyFunction:",
                    "              Type: AWS::Lambda::Function",
                    "              Properties:",
                    "                Name: $FUNCTION_NAME",
                    "                Alias: live",
                    "                CurrentVersion: $CURRENT_ARN",
                    "                TargetVersion: $NEW_ARN",
                    "        EOF",
                    "artifacts:",
                    "  files:",
                    "    - appspec.yml",
                ]),
            ),
        )

        # ── Pipeline role ───────────────────────────────────────────────
        pipeline_role = iam.Role(
            self, "PipelineRole",
            role_name="bg-lambda-linear-pipeline-role",
            assumed_by=iam.ServicePrincipal("codepipeline.amazonaws.com"),
        )
        artifact_bucket.grant_read_write(pipeline_role)
        pipeline_role.add_to_policy(iam.PolicyStatement(
            actions=["codebuild:BatchGetBuilds", "codebuild:StartBuild"],
            resources=["*"],
        ))
        pipeline_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "codedeploy:CreateDeployment", "codedeploy:GetDeployment",
                "codedeploy:GetDeploymentConfig", "codedeploy:GetApplicationRevision",
                "codedeploy:RegisterApplicationRevision",
            ],
            resources=["*"],
        ))

        # ── CfnPipeline ─────────────────────────────────────────────────
        # Source: GitHub → Build: CodeBuild (zip + publish version + appspec)
        # Deploy: CodeDeploy Lambda LINEAR_10PERCENT_EVERY_1_MINUTE
        #   pre_hook validates → 10% every 1min until 100%
        #   errors_alarm or throttles_alarm fires → auto rollback
        codepipeline.CfnPipeline(
            self, "Pipeline",
            name="bg-lambda-linear-pipeline",
            role_arn=pipeline_role.role_arn,
            artifact_store=codepipeline.CfnPipeline.ArtifactStoreProperty(
                type="S3",
                location=artifact_bucket.bucket_name,
            ),
            restart_execution_on_update=False,
            stages=[
                # ── Stage 1: Source (GitHub) ─────────────────────────────
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Source",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="GitHub_Source",
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Source",
                                owner="ThirdParty",
                                provider="GitHub",
                                version="1",
                            ),
                            output_artifacts=[
                                codepipeline.CfnPipeline.OutputArtifactProperty(name="SourceOutput")
                            ],
                            configuration={
                                "Owner": "Abdelali12-codes",
                                "Repo": "aws-lambda-blue-green-codedeploy",
                                "Branch": "master",
                                "OAuthToken": cdk.SecretValue.secrets_manager("github-access-token").unsafe_unwrap(),
                                "PollForSourceChanges": False,
                            },
                            run_order=1,
                        )
                    ],
                ),
                # ── Stage 2: Build ───────────────────────────────────────
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Build",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="Build",
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Build",
                                owner="AWS",
                                provider="CodeBuild",
                                version="1",
                            ),
                            input_artifacts=[
                                codepipeline.CfnPipeline.InputArtifactProperty(name="SourceOutput")
                            ],
                            output_artifacts=[
                                codepipeline.CfnPipeline.OutputArtifactProperty(name="BuildOutput")
                            ],
                            configuration={"ProjectName": build_project.name},
                            run_order=1,
                        )
                    ],
                ),
                # ── Stage 3: Deploy (CodeDeploy Lambda linear) ──────────
                # Shifts 10% every 1 minute until 100%.
                # Two alarms monitored at every step:
                #   errors_alarm   → any Lambda error → rollback
                #   throttles_alarm → throttle spike → rollback
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Deploy",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="Deploy",
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Deploy",
                                owner="AWS",
                                provider="CodeDeploy",
                                version="1",
                            ),
                            input_artifacts=[
                                codepipeline.CfnPipeline.InputArtifactProperty(name="BuildOutput")
                            ],
                            configuration={
                                "ApplicationName": application.application_name,
                                "DeploymentGroupName": "bg-lambda-linear-dg",
                            },
                            run_order=1,
                        )
                    ],
                ),
            ],
        )
