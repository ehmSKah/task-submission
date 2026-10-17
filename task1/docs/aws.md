# Running on AWS

The same CDK stack that runs on Floci deploys unchanged to AWS ([ADR 0010](adr/0010-infrastructure-and-environments.md)).
Two AWS environments are defined in `infra/config/`:

- **`aws-dev`**: disposable. It seeds the demo users, has the test fixtures and the WAF test
  rule, and is destroyed after each run.
- **`aws-prod`**: the hardened example. It keeps its data, is termination protected, and has no
  demo users or test fixtures.

Nothing here has been run against AWS yet; [`verification.md`](verification.md) lists what the
first deploy has to confirm.

## Account and region

- **Region:** each environment's config names it (`us-east-1` for both). The Makefile exports it
  as `AWS_REGION` for every AWS target.
- **Account:** no config names one, so no account id is kept in the repository. The stack is
  synthesised without an account (`aws://unknown-account/us-east-1` in the CDK manifest), and
  CloudFormation deploys it to the account of the caller's credentials. As a result, `cdk synth`
  and `make lint` need no AWS credentials.
- **Checks:**
  - `make doctor ENV=aws-dev` checks the account before anything is created: the credentials
    resolve, the API Gateway logging role is set, CDK is bootstrapped. It never prints the
    account id or the caller.
  - Every target that touches an environment first refuses anything that would send it
    elsewhere: an endpoint override (`AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_<SERVICE>`),
    Floci's dummy key, or another region. `ENV=local` always gets Floci's endpoint and dummy
    credentials.

## One-off setup per account and region

The owner of the account does these once, with administrator credentials.

1. **Bootstrap CDK:** `make bootstrap ENV=aws-dev`. Like any bootstrap, this gives
   CloudFormation's execution role `AdministratorAccess` by default. Narrowing it with
   `--cloudformation-execution-policies` is a production step.
2. **Set API Gateway's CloudWatch Logs role.** Access logs need it, and it is a setting of the
   account and region, not of a stack:

   ```sh
   aws iam create-role --role-name apigateway-cloudwatch-logs \
     --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
       "Principal":{"Service":"apigateway.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
   aws iam attach-role-policy --role-name apigateway-cloudwatch-logs \
     --policy-arn arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs
   aws apigateway update-account --region us-east-1 --patch-operations \
     op=replace,path=/cloudwatchRoleArn,value=arn:aws:iam::<ACCOUNT_ID>:role/apigateway-cloudwatch-logs
   ```

3. **For the workflow:** the GitHub OIDC provider, the role and the GitHub settings below.

## Running from a workstation

```sh
export AWS_PROFILE=<your profile>   # or any other credentials the AWS SDKs find
make up ENV=aws-dev                 # doctor, bootstrap (idempotent), deploy, endpoints, seed
make test-integration ENV=aws-dev   # the aws_only tests included
make contract-test ENV=aws-dev
make down ENV=aws-dev               # cdk destroy
```

- **The edge-optimized endpoint** adds a few minutes to the deploy and to the destroy.
- **`make down ENV=aws-prod`** is refused by termination protection. If the stack is ever
  deleted, its retained resources stay behind and are removed by hand: the orders table and the
  user pool (deletion protection must be turned off first), and the log groups.

## The `aws-e2e` workflow

[`aws-e2e.yml`](../../.github/workflows/aws-e2e.yml) runs only on a manual trigger, one run at a
time. It runs these steps:

1. Checks the account.
2. Deploys `aws-dev` and seeds the demo users.
3. Runs the integration suite (with the `aws_only` tests), then the contract suite.
4. Uploads the JUnit reports, if they hold no token.
5. **Always** destroys the stack, with freshly issued credentials.

`build/endpoints.json` is not uploaded, because its ARNs name the account.

### GitHub settings

- **Repository variable `AWS_E2E_ROLE_ARN`:** the role's ARN. It is a variable, not a secret:
  the ARN grants nothing without the trust policy.
- **Environment `aws-dev`:** the job runs in it, and the role trusts only tokens issued for it.
  Restrict its deployment branches to `main`, and add required reviewers if runs should be
  approved.

### OIDC provider (once per account)

IAM identity provider `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.

### The role's trust policy

Only workflow runs of this repository in the `aws-dev` environment can assume the role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:<OWNER>/<REPOSITORY>:environment:aws-dev"
        }
      }
    }
  ]
}
```

- **Without the environment:** the subject would be the branch,
  `repo:<OWNER>/<REPOSITORY>:ref:refs/heads/main`. Requiring the environment lets its
  protection rules decide who may run the workflow.
- **Session length:** one hour is enough for each credential step (the role's default maximum).

### The role's permissions

**Keep production in another account.** The policy below is scoped to `aws-dev` where it can be, but deploying through CDK's bootstrap roles means CloudFormation acts with the bootstrap's execution role, which can change any stack in the account, a production one included. Only the account boundary keeps this workflow away from `aws-prod`; the workflow itself only ever names `aws-dev`. The Cognito admin calls below cover every user pool in the account for the same reason, since the pool's id is only known after the deploy.

CloudFormation does the deploying through CDK's bootstrap roles. The workflow role only assumes
those roles, plus what the preflight check, the seeding and the tests call directly. In the
policy, `<REGION>` is `us-east-1` and `<ACCOUNT_ID>` is the account.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DeployThroughTheCdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::<ACCOUNT_ID>:role/cdk-hnb659fds-deploy-role-<ACCOUNT_ID>-<REGION>",
        "arn:aws:iam::<ACCOUNT_ID>:role/cdk-hnb659fds-file-publishing-role-<ACCOUNT_ID>-<REGION>"
      ]
    },
    {
      "Sid": "PreflightReadsTheAccountSetup",
      "Effect": "Allow",
      "Action": ["apigateway:GET", "ssm:GetParameter"],
      "Resource": [
        "arn:aws:apigateway:<REGION>::/account",
        "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/cdk-bootstrap/hnb659fds/version"
      ]
    },
    {
      "Sid": "SeedAndSignInTestUsers",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminAddUserToGroup"
      ],
      "Resource": "arn:aws:cognito-idp:<REGION>:<ACCOUNT_ID>:userpool/*"
    },
    {
      "Sid": "ReadTheDemoUsersPassword",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:/orders-api/aws-dev/seed-users-*"
    },
    {
      "Sid": "SwitchTheRateLimits",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter"],
      "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT_ID>:parameter/orders-api/aws-dev/rate-limits"
    },
    {
      "Sid": "TestFixturesInTheTables",
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchWriteItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/OrdersApi-aws-dev-*",
        "arn:aws:dynamodb:<REGION>:<ACCOUNT_ID>:table/OrdersApi-aws-dev-*/index/*"
      ]
    },
    {
      "Sid": "ReadTheApiFunctionsLogs",
      "Effect": "Allow",
      "Action": [
        "cloudformation:DescribeStackResources",
        "lambda:GetFunctionConfiguration",
        "logs:FilterLogEvents"
      ],
      "Resource": [
        "arn:aws:cloudformation:<REGION>:<ACCOUNT_ID>:stack/OrdersApi-aws-dev/*",
        "arn:aws:lambda:<REGION>:<ACCOUNT_ID>:function:OrdersApi-aws-dev-*",
        "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:OrdersApi-aws-dev-*",
        "arn:aws:logs:<REGION>:<ACCOUNT_ID>:log-group:/aws/lambda/OrdersApi-aws-dev-*"
      ]
    },
    {
      "Sid": "SeeWhomWafIsLimiting",
      "Effect": "Allow",
      "Action": "wafv2:GetRateBasedStatementManagedKeys",
      "Resource": "arn:aws:wafv2:<REGION>:<ACCOUNT_ID>:regional/webacl/orders-api-aws-dev/*"
    }
  ]
}
```

- **What the test helpers do with these:**
  - empty the counter table (`Scan`, `BatchWriteItem`)
  - write orders straight to the table (`PutItem`)
  - read the `byOwner` index (`Query`)
  - read the API function's log lines
  - read which addresses WAF is limiting

  They use developer or CI credentials, never a Lambda role ([ADR 0012](adr/0012-observability-and-security-baseline.md)).
- **Not needed:** `sts:GetCallerIdentity`, which needs no permission, and Cognito's
  `InitiateAuth`, which the public app clients allow without credentials.
- **If a call is refused,** the run names the denied action. The list is to be confirmed by the
  first run.

## Cost

- **A run costs cents:** mostly pay per request, and the monthly fees (WAF's web ACL and its two
  rules in `aws-dev`, the demo users' secret) are charged pro rata by the hour. The prices, with
  their sources and date, are in the [design document, section 8](design.md#cost-model).
- **Per run,** the WAF test sends at most a few thousand requests to `/health`.
