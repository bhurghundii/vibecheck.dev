# Deploying to AWS ECS

This guide stands vibecheck up on AWS with one CloudFormation template, [`deploy/aws-ecs/vibecheck.yaml`](https://github.com/bhurghundii/vibecheck.dev/blob/main/deploy/aws-ecs/vibecheck.yaml). The app runs as a single ECS Fargate task behind an Application Load Balancer, its database is RDS PostgreSQL 16, and it grades answers with Claude on Amazon Bedrock. Bedrock is reached with the task's own IAM role, so there is no Anthropic API key and no AWS access key to store anywhere.

It's written for someone who hasn't used AWS much. Every step is an AWS CLI command you can copy, and the worked example uses your account's default VPC so you don't have to design a network first.

!!! note "Needs Bedrock support in the image"
    The stack runs the app with `MODEL_PROVIDER=bedrock`. Your image has to be built from a version of vibecheck that supports it; see [Amazon Bedrock](configuration.md#amazon-bedrock) in the configuration reference. Release images published before that support landed can't grade on Bedrock, which is one reason this guide builds the image from source.

## Prerequisites

- **An AWS account and the AWS CLI v2**, signed in with permission to create the stack's resources (for a first trial, an administrator user or role is simplest). Check with `aws sts get-caller-identity`.
- **A region**, picked once and used for everything below. The examples use `us-east-1`.
- **Bedrock access to the Claude model you'll grade with, in that region.** Anthropic models need a one-time use-case form per AWS account before the first call: open the [Bedrock console](https://console.aws.amazon.com/bedrock/home), pick the model in *Model catalog* and submit the form when asked. Then check:

    ```sh
    aws bedrock get-foundation-model-availability \
      --region us-east-1 --model-id anthropic.claude-sonnet-4-6
    ```

    `agreementAvailability.status` should be `AVAILABLE`. `PENDING` means AWS is still setting up the agreement; calls fail with `AccessDeniedException` until it changes.
- **A model ID for `BedrockModelId`.** In most regions Claude is served through a cross-region *inference profile* rather than an in-region model, so use a profile ID such as `us.anthropic.claude-sonnet-4-6`. List the ones your region offers with `aws bedrock list-inference-profiles --region us-east-1 --type-equals SYSTEM_DEFINED`.
- **A container image Fargate can pull**: in Amazon ECR in the same account and region (the next section builds one), or in a public registry. The template takes no registry credentials, so a private registry outside ECR won't work.
- **Docker**, if you're building the image yourself.

## Build and push the image

Run these from a checkout of vibecheck that includes Bedrock support. The ECR repository is created by hand, not by the stack, so it survives a stack delete; see [teardown](#teardown).

```sh
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com

aws ecr create-repository --region $REGION --repository-name vibecheck

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $REGISTRY

docker build --platform linux/amd64 -t $REGISTRY/vibecheck:latest .
docker push $REGISTRY/vibecheck:latest
```

`--platform linux/amd64` matters on an Apple Silicon Mac: Fargate runs x86 tasks by default, and an ARM image fails to start there. The image URI, `$REGISTRY/vibecheck:latest`, is what goes in the `ImageUri` parameter.

## Deploy the stack

Every AWS account has a default VPC in each region, with one public subnet per availability zone. The example puts the load balancer, the task and the database in those subnets:

```sh
VPC_ID=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --region $REGION \
  --filters Name=vpc-id,Values=$VPC_ID Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')

aws cloudformation deploy \
  --region $REGION \
  --stack-name vibecheck \
  --template-file deploy/aws-ecs/vibecheck.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    VpcId=$VPC_ID \
    LoadBalancerSubnetIds=$SUBNETS \
    ServiceSubnetIds=$SUBNETS \
    DatabaseSubnetIds=$SUBNETS \
    AssignPublicIp=ENABLED \
    ImageUri=$REGISTRY/vibecheck:latest \
    BedrockModelId=us.anthropic.claude-sonnet-4-6
```

The first deploy takes 10 to 20 minutes, most of it creating the database. What the parameters do:

| Parameter | In the example |
|---|---|
| `VpcId` | the default VPC |
| `LoadBalancerSubnetIds` | where the load balancer lives; at least two availability zones |
| `ServiceSubnetIds` | where the task runs |
| `DatabaseSubnetIds` | where RDS lives; at least two availability zones. The database is never publicly accessible and only accepts connections from the task |
| `AssignPublicIp` | `ENABLED`, because the default VPC has no NAT gateway: without a public IP the task can't reach ECR, Secrets Manager or Bedrock. Leave it at its default, `DISABLED`, when `ServiceSubnetIds` are private subnets with a NAT gateway |
| `ImageUri` | the image you pushed |
| `BedrockModelId` | the model or inference profile to grade with |

Optional parameters, all left unset above:

- `CertificateArn`: an ACM certificate for HTTPS. Read [HTTP is for trials only](#http-is-for-trials-only) before going without one.
- `PublicBaseUrl`: the URL people will use, if you put your own domain in front of the load balancer. Without it the stack uses the load balancer's DNS name.
- `GitHubStatusTokenSecretArn`: a Secrets Manager secret holding `GITHUB_STATUS_TOKEN`, so vibecheck can post the `vibecheck/verdict` status. Create it first with `aws secretsmanager create-secret --name vibecheck/github-status-token --secret-string file://github-token.txt` and pass the `ARN` it prints.
- `AllowedRepos`: the repos allowed to create sessions; see [configuration](configuration.md).
- `LoadBalancerScheme`: `internet-facing` by default. GitHub's hosted runners must reach the server, so keep it unless your runners are inside the VPC.

When the deploy finishes, read the outputs:

```sh
aws cloudformation describe-stacks --region $REGION --stack-name vibecheck \
  --query 'Stacks[0].Outputs' --output table
```

`BaseUrl` is the server's URL. `curl $BASE_URL/healthz` should return `{"status": "ok"}`.

## Wire up the repo

The stack generates `VIBECHECK_TOKEN` itself and keeps it in Secrets Manager; it never appears in the template, the parameters or the outputs. Read it with:

```sh
TOKEN_ARN=$(aws cloudformation describe-stacks --region $REGION --stack-name vibecheck \
  --query "Stacks[0].Outputs[?OutputKey=='VibecheckTokenSecretArn'].OutputValue" --output text)
aws secretsmanager get-secret-value --region $REGION --secret-id $TOKEN_ARN \
  --query SecretString --output text
```

Then [wire it into a repo](wire-it-up.md): the repo's `VIBECHECK_BASE_URL` secret is the `BaseUrl` output, and its `VIBECHECK_TOKEN` secret is the value printed above. Paste the token straight into GitHub; don't keep it in a file or your shell history.

## HTTP is for trials only

!!! warning
    Without `CertificateArn` the load balancer serves plain HTTP on port 80. Every session link vibecheck hands out is a credential (anyone holding it can answer the questions), and over HTTP those links, the answers and the bearer token from the workflow all cross the internet unencrypted. Use HTTP only to try the stack out, then redeploy with a certificate. With `CertificateArn` the stack serves HTTPS on 443 and redirects port 80 to it. The [before you make it public](security.md) checklist applies either way.

## What you pay for

The stack runs around the clock, so these are billed for as long as it exists, whether or not anyone opens a PR:

- **ECS Fargate**: one task's vCPU and memory, all the time.
- **Application Load Balancer**: hourly, plus load balancer capacity units.
- **RDS PostgreSQL**: the database instance, its encrypted storage and its automated backups.
- **RDS snapshot**: the final snapshot RDS takes when you delete the stack. It stays, and stays billed, until you delete it.
- **Secrets Manager**: each secret the stack creates (the generated `VIBECHECK_TOKEN` and the database connection secret), per month and per API call.
- **CloudWatch Logs**: ingestion and storage for the task's log group, kept 30 days.
- **Public IPv4 addresses**: the load balancer's, and the task's when `AssignPublicIp` is `ENABLED`.
- **Data transfer** out of AWS.
- **Bedrock**: per input and output token, each time an answer is graded. Billed through AWS Marketplace, so it shows up under the model provider rather than Bedrock in Cost Explorer.

Outside the stack, you also pay for the ECR repository's image storage and for any `GitHubStatusTokenSecretArn` secret you created. The ECS cluster, security groups, IAM roles and target group cost nothing on their own. To see exactly what the stack created, run `aws cloudformation list-stack-resources --region $REGION --stack-name vibecheck`.

## Troubleshooting

**Grading fails with `AccessDeniedException` from Bedrock.** The task logs (below) show the error. Check, in order:

1. *Model access.* Run the `get-foundation-model-availability` command from the prerequisites for the model behind your profile. Anything but `AVAILABLE` means the use-case form isn't done or AWS is still processing it.
2. *IAM.* The task role may call `bedrock:InvokeModel` on `BedrockModelId` and nothing else. If you changed the model the app uses without changing the parameter, redeploy with the new `BedrockModelId`. An organization service control policy that denies Bedrock also shows up as `AccessDeniedException`.
3. *Inference-profile foundation models.* A profile such as `us.anthropic.claude-sonnet-4-6` routes each call to the underlying foundation model in one of several regions, and the call needs permission on that model in whichever region it lands. List them with `aws bedrock get-inference-profile --region $REGION --inference-profile-identifier us.anthropic.claude-sonnet-4-6 --query 'models[].modelArn'`. The template grants the foundation models behind the profile; if a destination region is blocked by a service control policy, or a region is disabled for the account, some calls fail and others succeed.

**The task keeps failing load balancer health checks.** The load balancer checks `/healthz`, which fails when the app can't reach its database or its configuration doesn't validate. Read the app's own output from the log group in the `LogGroupName` output:

```sh
LOG_GROUP=$(aws cloudformation describe-stacks --region $REGION --stack-name vibecheck \
  --query "Stacks[0].Outputs[?OutputKey=='LogGroupName'].OutputValue" --output text)
aws logs tail $LOG_GROUP --region $REGION --since 30m
```

ECS's own view of why tasks stopped is in the service events: `aws ecs describe-services --region $REGION --cluster <ClusterName> --services <ServiceName> --query 'services[0].events[:5]'`, with the names from the stack outputs.

**The task never starts: `CannotPullContainerError` or `ResourceInitializationError`.** Fargate couldn't fetch the image or the secrets. The usual causes:

- The task has no route to the internet: `AssignPublicIp` is `DISABLED` in subnets with no NAT gateway, such as the default VPC's. Redeploy with `AssignPublicIp=ENABLED`, or use private subnets with a NAT gateway.
- `ImageUri` is wrong, or the tag was never pushed. Compare it with `aws ecr describe-images --region $REGION --repository-name vibecheck`.
- The image is in a private registry other than ECR, which the template has no credentials for.

## Teardown

Delete the stack, then the two things it leaves behind:

```sh
aws cloudformation delete-stack --region $REGION --stack-name vibecheck
aws cloudformation wait stack-delete-complete --region $REGION --stack-name vibecheck

# The database's final snapshot is retained on purpose, so a delete can't lose data.
aws rds describe-db-snapshots --region $REGION --snapshot-type manual \
  --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotCreateTime]' --output table
aws rds delete-db-snapshot --region $REGION --db-snapshot-identifier <the snapshot from the stack>

aws ecr delete-repository --region $REGION --repository-name vibecheck --force
```

`--force` deletes the images in the repository too. If you created a secret for `GitHubStatusTokenSecretArn`, delete it with `aws secretsmanager delete-secret`.
