# Manual IAM changes for FloodPulse

Everything in this folder is a **reference document for a human operator**. Nothing here is applied
by `infra/deploy.ps1`, by any test, or by an agent working in this repository.

## Why this folder exists

`POST /subscriptions` lets a visitor ask for flood alerts by email. The handler calls
`sns.subscribe(TopicArn=…, Protocol="email", Endpoint=<validated address>, ReturnSubscriptionArn=True)`
with the topic ARN read **only** from the Lambda's own environment variable
(`FLOODPULSE_SNS_TOPIC_ARN`). That call needs one permission the deployed role does not have:

| Policy | Action | Resource |
| --- | --- | --- |
| `FloodPulseDynamoDBAccess` | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` | `arn:aws:dynamodb:eu-north-1:725934295966:table/floodpulse` |
| `FloodPulseSnsPublish` | `sns:Publish` | `arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts` |
| *(needed)* | **`sns:Subscribe`** on the **same single topic** | `arn:aws:sns:eu-north-1:725934295966:floodpulse-alerts` |

`iam:PutRolePolicy` is denied for the `cloud-project` user, and the brief for this milestone forbids an
agent from editing IAM. The route therefore returns **503** with a friendly message until an operator
grants the permission: a missing permission degrades one public form; it never breaks the dashboard,
ingestion or alert publishing.

## The exact manual action

> **Manual approval required: allow sns:Subscribe only on floodpulse-alerts.**

```powershell
aws iam put-role-policy --role-name floodpulse-lambda-role `
  --policy-name FloodPulseSnsPublish `
  --policy-document file://infra/iam/floodpulse-sns-publish-and-subscribe.json `
  --profile cloud-project --region eu-north-1
```

That replaces the existing `FloodPulseSnsPublish` inline policy with
[`floodpulse-sns-publish-and-subscribe.json`](floodpulse-sns-publish-and-subscribe.json), which is the
current statement **plus one action**:

* `Action` becomes `["sns:Publish", "sns:Subscribe"]`
* `Resource` stays the one topic ARN — no `*`, no other topic, no wildcard region
* no new policy, no managed policy, no trust-policy change, no `sns:*`
* `sns:Unsubscribe`, `sns:CreateTopic`, `sns:SetTopicAttributes` and everything else stay denied

To confirm what is attached at any time:

```powershell
aws iam get-role-policy --role-name floodpulse-lambda-role `
  --policy-name FloodPulseSnsPublish --profile cloud-project --region eu-north-1 `
  --query "PolicyDocument.Statement[].{Action:Action,Resource:Resource}"
```

## After the approval

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File infra/deploy.ps1
python tools/verify_deployed_code.py   # now byte-identical to src/
python tools/live_acceptance.py        # includes the POST /subscriptions probe
```

The deploy script prints this same instruction when it cannot attach the policy itself
(`Write-Warn2 'operator action: …'`), and it never widens the role beyond `sns:Publish` on its own. A
test in `tests/test_deploy_script.py` fails if a later edit makes the automated document wider.

## Rollback

The previous state is the same command with an `Action` of `"sns:Publish"` only, or deleting the
policy while it is unattached from anything else:

```powershell
aws iam put-role-policy --role-name floodpulse-lambda-role --policy-name FloodPulseSnsPublish `
  --policy-document file://build/iam-sns-publish.json --profile cloud-project --region eu-north-1
```

(`build/iam-sns-publish.json` is the Publish-only document the deploy script writes.)
