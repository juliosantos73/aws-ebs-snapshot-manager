import json
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Literal

import boto3
from botocore.config import Config

TAG_KEY = os.environ.get('TAG_KEY', 'ScheduledSnapshot')
TAG_VALUE = os.environ.get('TAG_VALUE', 'True')
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '7'))
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'

# Tag applied to every snapshot we create — used to identify them during cleanup
MANAGER_TAG = 'aws-ebs-snapshot-manager'

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOTO_CONFIG = Config(retries={'max_attempts': 3, 'mode': 'adaptive'})

Action = Literal['create', 'cleanup', 'all']


def create_snapshots(ec2_client, region: str, dry_run: bool) -> list[str]:
    paginator = ec2_client.get_paginator('describe_volumes')
    snapshot_ids = []

    for page in paginator.paginate(
        Filters=[
            {'Name': f'tag:{TAG_KEY}', 'Values': [TAG_VALUE]},
            {'Name': 'status', 'Values': ['in-use', 'available']},
        ]
    ):
        for volume in page['Volumes']:
            volume_id = volume['VolumeId']

            tags = [
                {'Key': 'ManagedBy', 'Value': MANAGER_TAG},
                {'Key': 'SourceVolumeId', 'Value': volume_id},
                {'Key': 'RetentionDays', 'Value': str(RETENTION_DAYS)},
            ]
            # Copy Name tag from volume so snapshot is identifiable in the console
            for tag in volume.get('Tags', []):
                if tag['Key'] == 'Name':
                    tags.append({'Key': 'Name', 'Value': f"snapshot-{tag['Value']}"})
                    break

            logger.info(json.dumps({
                'region': region, 'action': 'create_snapshot',
                'volume_id': volume_id, 'dry_run': dry_run,
            }))

            if not dry_run:
                try:
                    resp = ec2_client.create_snapshot(
                        VolumeId=volume_id,
                        Description=f"Automated snapshot of {volume_id} — {MANAGER_TAG}",
                        TagSpecifications=[{'ResourceType': 'snapshot', 'Tags': tags}],
                    )
                    snapshot_ids.append(resp['SnapshotId'])
                    logger.info(json.dumps({
                        'region': region, 'action': 'create_snapshot',
                        'volume_id': volume_id, 'snapshot_id': resp['SnapshotId'],
                    }))
                except Exception:
                    logger.error(json.dumps({
                        'region': region, 'action': 'create_snapshot',
                        'volume_id': volume_id, 'error': traceback.format_exc(),
                    }))
            else:
                snapshot_ids.append(f"dry-run:{volume_id}")

    return snapshot_ids


def cleanup_snapshots(ec2_client, region: str, dry_run: bool) -> list[str]:
    paginator = ec2_client.get_paginator('describe_snapshots')
    deleted_ids = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    for page in paginator.paginate(
        OwnerIds=['self'],
        Filters=[{'Name': 'tag:ManagedBy', 'Values': [MANAGER_TAG]}],
    ):
        for snapshot in page['Snapshots']:
            if snapshot['StartTime'] >= cutoff:
                continue

            snapshot_id = snapshot['SnapshotId']
            age_days = (datetime.now(timezone.utc) - snapshot['StartTime']).days

            logger.info(json.dumps({
                'region': region, 'action': 'delete_snapshot',
                'snapshot_id': snapshot_id, 'age_days': age_days, 'dry_run': dry_run,
            }))

            if not dry_run:
                try:
                    ec2_client.delete_snapshot(SnapshotId=snapshot_id)
                    deleted_ids.append(snapshot_id)
                except Exception:
                    logger.error(json.dumps({
                        'region': region, 'action': 'delete_snapshot',
                        'snapshot_id': snapshot_id, 'error': traceback.format_exc(),
                    }))
            else:
                deleted_ids.append(snapshot_id)

    return deleted_ids


def manage_region(region_name: str, action: Action, dry_run: bool) -> dict:
    ec2 = boto3.client('ec2', region_name=region_name, config=BOTO_CONFIG)
    result = {'created': [], 'deleted': []}

    if action in ('create', 'all'):
        result['created'] = create_snapshots(ec2, region_name, dry_run)

    if action in ('cleanup', 'all'):
        result['deleted'] = cleanup_snapshots(ec2, region_name, dry_run)

    return result


def lambda_handler(event: dict, context) -> dict:
    action = str(event.get('action', 'all')).lower()
    if action not in ('create', 'cleanup', 'all'):
        raise ValueError(f"Invalid action: '{action}'. Expected 'create', 'cleanup', or 'all'.")

    dry_run = bool(event.get('dry_run', DRY_RUN))

    logger.info(json.dumps({
        'action': action, 'dry_run': dry_run, 'retention_days': RETENTION_DAYS,
    }))

    ec2_global = boto3.client('ec2', config=BOTO_CONFIG)
    regions = ec2_global.describe_regions(
        Filters=[{'Name': 'opt-in-status', 'Values': ['opt-in-not-required', 'opted-in']}]
    )['Regions']

    start_time = datetime.now()
    results = {}

    for region in regions:
        region_name = region['RegionName']
        try:
            region_result = manage_region(region_name, action, dry_run)
            if region_result['created'] or region_result['deleted']:
                results[region_name] = region_result
        except Exception:
            logger.error(json.dumps({
                'region': region_name, 'action': action,
                'error': traceback.format_exc(),
            }))

    elapsed = round((datetime.now() - start_time).total_seconds(), 2)
    total_created = sum(len(v['created']) for v in results.values())
    total_deleted = sum(len(v['deleted']) for v in results.values())

    logger.info(json.dumps({
        'elapsed_seconds': elapsed,
        'total_created': total_created,
        'total_deleted': total_deleted,
    }))

    return {
        'statusCode': 200,
        'body': json.dumps({
            'action': action,
            'dry_run': dry_run,
            'retention_days': RETENTION_DAYS,
            'results': results,
        }),
    }
