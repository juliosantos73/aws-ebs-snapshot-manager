# aws-ebs-snapshot-manager

🌐 [English](README.md) | [Português](README.pt-BR.md)

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![AWS Lambda](https://img.shields.io/badge/AWS-Lambda-orange?logo=amazon-aws)

An AWS Lambda function that **automatically creates EBS snapshots** for tagged volumes and **deletes expired snapshots** based on a configurable retention policy. Runs daily across all regions via EventBridge.

---

## How it works

A single EventBridge rule triggers the Lambda once a day. In each execution the function:

1. **Creates** snapshots for all EBS volumes tagged with `ScheduledSnapshot = True`
2. **Deletes** snapshots older than `RETENTION_DAYS` that were previously created by this manager

```
EventBridge (daily cron) ──► Lambda
                                 │
                                 ├── describe_volumes (tag: ScheduledSnapshot=True)
                                 │       └──► create_snapshot  ──► tags snapshot with ManagedBy
                                 │
                                 └── describe_snapshots (tag: ManagedBy=aws-ebs-snapshot-manager)
                                         └──► delete if older than RETENTION_DAYS
```

> Only snapshots created by this manager (tagged `ManagedBy = aws-ebs-snapshot-manager`) are ever deleted. Manually created snapshots are never touched.

---

## Features

- Creates daily snapshots for all tagged EBS volumes across all regions
- Deletes only **managed snapshots** — manually created snapshots are safe
- Configurable **retention policy** via environment variable
- Copies the volume `Name` tag to the snapshot for easy identification in the console
- Supports `create`, `cleanup`, or `all` (default) actions independently
- Server-side filtering — only tagged volumes are returned by the API
- Paginated API calls — works correctly at any scale
- Adaptive retry — handles AWS API throttling automatically
- Dry run mode — lists what would be created/deleted without executing
- Structured JSON logs — compatible with CloudWatch Insights queries

---

## Prerequisites

- An AWS account
- Python 3.12+ (for local development only)
- AWS CLI configured (optional, for CLI-based deployment)

---

## 1. Tag your EBS volumes

Add the following tag to every volume you want snapshotted:

| Key                | Value  |
|--------------------|--------|
| `ScheduledSnapshot`| `True` |

> Volumes **without** this tag are ignored entirely.

---

## 2. Create the IAM execution role

**Policy document** — save as `ebs-snapshot-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:CreateSnapshot",
        "ec2:DeleteSnapshot",
        "ec2:CreateTags"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

**AWS CLI:**

```bash
aws iam create-role \
  --role-name ebs-snapshot-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam put-role-policy \
  --role-name ebs-snapshot-role \
  --policy-name ebs-snapshot-policy \
  --policy-document file://ebs-snapshot-policy.json
```

---

## 3. Deploy the Lambda function

### Option A — AWS Console

1. Go to **Lambda → Create function**
2. Name: `ebs-snapshot-manager` | Runtime: **Python 3.12** | Architecture: `x86_64`
3. Execution role: select the role created in step 2
4. Upload `lambda_function.py` (or paste the code in the inline editor)
5. Handler: `lambda_function.lambda_handler`
6. Timeout: **5 minutes** (multi-region scans take time)
7. Save

### Option B — AWS CLI

```bash
zip lambda_function.zip lambda_function.py

ROLE_ARN=$(aws iam get-role --role-name ebs-snapshot-role --query Role.Arn --output text)

aws lambda create-function \
  --function-name ebs-snapshot-manager \
  --runtime python3.12 \
  --role "$ROLE_ARN" \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://lambda_function.zip \
  --timeout 300

# To update after changes:
aws lambda update-function-code \
  --function-name ebs-snapshot-manager \
  --zip-file fileb://lambda_function.zip
```

---

## 4. Configure the EventBridge rule

A single daily rule handles both creation and cleanup.

### AWS Console

1. Go to **EventBridge → Rules → Create rule**
2. Select **Schedule** | Cron: `cron(0 2 * * ? *)` (runs at 02:00 UTC daily)
3. Target: **Lambda function** → `ebs-snapshot-manager`
4. Input: leave as default — no payload required

### AWS CLI

```bash
LAMBDA_ARN=$(aws lambda get-function --function-name ebs-snapshot-manager --query Configuration.FunctionArn --output text)

aws events put-rule \
  --name EBSSnapshotManager \
  --schedule-expression "cron(0 2 * * ? *)" \
  --state ENABLED

aws events put-targets \
  --rule EBSSnapshotManager \
  --targets "Id=1,Arn=$LAMBDA_ARN"

aws lambda add-permission \
  --function-name ebs-snapshot-manager \
  --statement-id AllowEventBridge \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn $(aws events describe-rule --name EBSSnapshotManager --query RuleArn --output text)
```

---

## Configuration

| Variable          | Default              | Description                                                |
|-------------------|----------------------|------------------------------------------------------------|
| `TAG_KEY`         | `ScheduledSnapshot`  | Tag key used to identify volumes to snapshot               |
| `TAG_VALUE`       | `True`               | Expected tag value (case-sensitive)                        |
| `RETENTION_DAYS`  | `7`                  | Number of days to keep managed snapshots before deleting   |
| `DRY_RUN`         | `false`              | Set to `true` to log actions without executing them        |

**AWS CLI:**

```bash
aws lambda update-function-configuration \
  --function-name ebs-snapshot-manager \
  --environment "Variables={TAG_KEY=ScheduledSnapshot,TAG_VALUE=True,RETENTION_DAYS=7,DRY_RUN=false}"
```

---

## Testing

### Dry run via Lambda console

Go to **Lambda → Test** and use the following payload to simulate a full cycle without creating or deleting anything:

```json
{ "action": "all", "dry_run": true }
```

To test each action independently:

```json
{ "action": "create", "dry_run": true }
```

```json
{ "action": "cleanup", "dry_run": true }
```

### Dry run via AWS CLI

```bash
aws lambda invoke \
  --function-name ebs-snapshot-manager \
  --payload '{"action":"all","dry_run":true}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

---

## Example response

```json
{
  "action": "all",
  "dry_run": false,
  "retention_days": 7,
  "results": {
    "us-east-1": {
      "created": ["snap-0a1b2c3d4e5f6a7b8"],
      "deleted": ["snap-0z9y8x7w6v5u4t3s2"]
    },
    "sa-east-1": {
      "created": ["snap-0b2c3d4e5f6a7b8c9"],
      "deleted": []
    }
  }
}
```

---

## Managed snapshot tags

Every snapshot created by this manager is tagged automatically:

| Tag                  | Value                          |
|----------------------|--------------------------------|
| `ManagedBy`          | `aws-ebs-snapshot-manager`     |
| `SourceVolumeId`     | The volume ID (e.g. `vol-...`) |
| `RetentionDays`      | The configured retention value |
| `Name`               | `snapshot-<volume-name>` (if volume has a Name tag) |

---

## Monitoring

**All snapshots created today:**

```
fields @timestamp, region, volume_id, snapshot_id
| filter action = "create_snapshot" and not ispresent(error)
| sort @timestamp desc
```

**All snapshots deleted today:**

```
fields @timestamp, region, snapshot_id, age_days
| filter action = "delete_snapshot" and not ispresent(error)
| sort @timestamp desc
```

**Errors:**

```
fields @timestamp, region, action, volume_id, snapshot_id, error
| filter ispresent(error)
| sort @timestamp desc
```

---

## Local development

```bash
pip install -r requirements-dev.txt

# Dry run locally (requires AWS credentials configured)
python -c "
from lambda_function import lambda_handler
result = lambda_handler({'action': 'all', 'dry_run': True}, None)
print(result)
"
```

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'Add your feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

Please keep PRs focused — one feature or fix per PR.

---

## License

[MIT](LICENSE) — © Júlio César Santos
