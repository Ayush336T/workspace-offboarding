import base64
import json
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

import config
from main import get_credentials, get_suspended_users, send_slack_notification


CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "3"))


def chunk_list(items, size):
    return [items[i : i + size] for i in range(0, len(items), size)]


def main():
    credentials = get_credentials()
    admin_service = build("admin", "directory_v1", credentials=credentials)

    print(f"Domain: {config.DOMAIN}")
    print(f"Suspension threshold: {config.SUSPENSION_THRESHOLD_DAYS} days")
    print(f"Chunk size: {CHUNK_SIZE}")

    suspended_users = get_suspended_users(admin_service)
    print(f"Found {len(suspended_users)} users suspended for {config.SUSPENSION_THRESHOLD_DAYS}+ days")

    emails = [u["primaryEmail"] for u in suspended_users]

    if config.SKIP_USERS:
        before = len(emails)
        emails = [e for e in emails if e.lower() not in config.SKIP_USERS]
        removed = before - len(emails)
        if removed:
            print(f"Skipping {removed} user(s) from SKIP_USERS")

    if config.BATCH_SIZE > 0:
        emails = emails[: config.BATCH_SIZE]
        print(f"Batch cap: {config.BATCH_SIZE} — processing {len(emails)} user(s)")

    shards = chunk_list(emails, CHUNK_SIZE) if emails else []
    print(f"Produced {len(shards)} shard(s) of up to {CHUNK_SIZE} user(s) each")

    if emails:
        send_slack_notification(
            f":rocket: *Offboarding started* — processing {len(emails)} user(s) suspended {config.SUSPENSION_THRESHOLD_DAYS}+ days across {len(shards)} shard(s)."
        )
    else:
        send_slack_notification(
            f":information_source: *Offboarding run complete* — no users to process (0 suspended {config.SUSPENSION_THRESHOLD_DAYS}+ days)."
        )

    # base64-encode shard payload so GitHub's secret-masking (which
    # matches the org domain from GOOGLE_DOMAIN) doesn't redact the
    # emails and break matrix expansion.
    shards_b64 = base64.b64encode(json.dumps(shards).encode()).decode()
    shard_indices = list(range(len(shards)))

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"shards_b64={shards_b64}\n")
            f.write(f"shard_indices={json.dumps(shard_indices)}\n")
            f.write(f"has_work={'true' if shards else 'false'}\n")
            f.write(f"total_users={len(emails)}\n")
    else:
        print(json.dumps(shards))


if __name__ == "__main__":
    main()
