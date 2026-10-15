# FloodPulse Agent Rules

## Mission

Build FloodPulse: a live AWS-based flash-flood early-warning and risk-prediction system.

The finished project must be a genuinely working cloud application, not a mockup.

Core capabilities:
- simulate environmental sensors across multiple geographic zones
- continuously ingest sensor readings
- calculate flood risk for every zone
- classify risk as NORMAL, WATCH, WARNING, or CRITICAL
- identify the highest-risk zone
- estimate time-to-threshold where technically reasonable
- create flood incidents automatically
- show live and historical conditions in a web dashboard
- issue AWS-based alerts
- support a reliable live demonstration

## Hard boundaries

- Work only inside this repository unless explicitly told otherwise.
- Never access or modify any unrelated repository.
- Never access CareConnect.
- Never use the AWS root account.
- Use AWS CLI profile `cloud-project` for AWS operations.
- AWS region is `eu-north-1`.
- Never expose, print, commit, or store credentials or secrets.
- Never commit `.env`, access keys, secret keys, tokens, PEM files, or passwords.
- Never fabricate command results, tests, deployment results, measurements, or AWS outputs.
- Never claim something works unless it was actually verified.
- Never delete AWS resources unless explicitly instructed.
- Never force-push Git unless explicitly instructed.
- Do not provision expensive AWS resources without explicit approval.

## AWS cost rules

We have limited AWS credits.

Prefer low-cost/serverless architecture.

Preferred services:
- API Gateway
- Lambda
- DynamoDB
- S3
- SNS
- CloudWatch

IoT Core may be added only if its value justifies the additional complexity.

Avoid unless explicitly approved:
- GPU EC2
- large EC2 instances
- RDS
- EKS
- OpenSearch
- SageMaker endpoints
- NAT Gateway
- other continuously billed infrastructure

Before provisioning new AWS infrastructure:
1. state what will be created
2. explain why it is required
3. estimate likely cost
4. wait for approval when the change is substantial or costly

## Local hardware constraints

Development machine:
- Intel i7
- 16 GB RAM
- RTX 2050 4 GB
- limited local disk space

Therefore:
- do not download large datasets unnecessarily
- do not download local LLMs
- do not introduce GPU-heavy processing
- avoid Docker unless it materially helps
- keep dependency footprint reasonable
- prefer AWS storage for substantial persistent data

## Engineering priorities

Use this priority order:

1. working end-to-end system
2. correctness
3. demo reliability
4. cloud architecture
5. security
6. testing
7. visual polish
8. optional advanced features

Do not overengineer.

Because the deadline is short, always prefer a simpler complete implementation over a sophisticated incomplete implementation.

## Development workflow

Before substantial work:
1. inspect existing code and git status
2. understand the current architecture
3. give a concise plan
4. make the smallest coherent change
5. run relevant checks/tests
6. report exactly what passed and failed

Preserve working functionality.

Make small logical commits.

Keep `main` usable.

Do not rewrite unrelated files.

## FloodPulse scientific rules

Initial sensor variables should focus on:
- rainfall intensity
- drain or drainage-channel level
- soil saturation
- water level
- flow rate

All synthetic data must be explicitly identified as simulated.

Flood-risk calculations must be explainable.

Do not falsely claim that heuristic calculations are scientifically validated prediction models.

Clearly distinguish:
- measured data
- simulated data
- heuristic risk scores
- statistical forecasts
- machine-learning predictions

Do not introduce machine learning until the deterministic end-to-end system works.

## MVP architecture principle

Prefer the shortest reliable path to a live system.

Initial target architecture:

Sensor simulator
-> API Gateway
-> Lambda
-> DynamoDB
-> API
-> Web dashboard

Additional supporting services:
- SNS for alerts
- CloudWatch for monitoring
- S3 for historical/archive data where useful

Do not add infrastructure merely to increase the number of AWS services used.

## Security

Validate all externally supplied payloads.

Use least-privilege AWS permissions where practical.

Do not put AWS credentials in application source code.

Do not expose administrative endpoints publicly without protection.

Do not disable security controls simply to make deployment easier.

## Deadline rule

Build vertically.

First make:
one sensor -> AWS -> risk calculation -> database -> dashboard

Then expand to:
multiple zones -> incidents -> alerts -> history -> polish

Do not start multiple unfinished subsystems at once.

## Communication

Keep progress reports concise.

Do not repeatedly restate these rules.

When uncertain about a consequential architecture or cost decision, ask before proceeding.
