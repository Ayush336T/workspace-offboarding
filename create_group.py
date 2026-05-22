import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

import config

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group",
]

MANAGER_EMAIL = os.environ.get("MANAGER_EMAIL", "adnan.bhutta@devrev.ai")
GROUP_EMAIL = os.environ.get("GROUP_EMAIL", "Partnership@devrev.ai")
GROUP_NAME = os.environ.get("GROUP_NAME", "Partnership")


def get_credentials():
    if config.SERVICE_ACCOUNT_JSON:
        info = json.loads(config.SERVICE_ACCOUNT_JSON)
    else:
        with open("service_account.json", "r") as f:
            info = json.load(f)

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    return credentials.with_subject(config.ADMIN_EMAIL)


def get_direct_reports(admin_service, manager_email):
    """Get all direct reports of a manager by scanning users and checking relations."""
    reports = []
    page_token = None
    debug_printed = False

    while True:
        results = (
            admin_service.users()
            .list(
                domain=config.DOMAIN,
                maxResults=500,
                pageToken=page_token,
                projection="full",
            )
            .execute()
        )

        for user in results.get("users", []):
            # Print first user's full structure for debugging
            if not debug_printed:
                print(f"  DEBUG - Sample user fields: {list(user.keys())}")
                relations = user.get("relations", [])
                if relations:
                    print(f"  DEBUG - Sample relations: {relations}")
                debug_printed = True

            # Check relations field for manager
            relations = user.get("relations", [])
            for rel in relations:
                rel_value = rel.get("value", "").lower()
                if rel.get("type") == "manager" and (
                    rel_value == manager_email.lower()
                    or manager_email.lower().split("@")[0] in rel_value
                ):
                    reports.append(user)
                    break

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return reports


def create_group(admin_service, group_email, group_name):
    """Create a Google Group."""
    group_body = {
        "email": group_email,
        "name": group_name,
        "description": f"Group for {group_name} team",
    }

    try:
        group = admin_service.groups().insert(body=group_body).execute()
        print(f"Created group: {group['email']}")
        return group
    except Exception as e:
        if "Entity already exists" in str(e):
            print(f"Group {group_email} already exists, continuing...")
            group = admin_service.groups().get(groupKey=group_email).execute()
            return group
        raise


def add_member_to_group(admin_service, group_email, member_email):
    """Add a member to a group."""
    member_body = {
        "email": member_email,
        "role": "MEMBER",
    }

    try:
        admin_service.members().insert(groupKey=group_email, body=member_body).execute()
        print(f"  Added: {member_email}")
    except Exception as e:
        if "Member already exists" in str(e):
            print(f"  Already a member: {member_email}")
        else:
            print(f"  ERROR adding {member_email}: {e}")


def main():
    print(f"Creating group: {GROUP_EMAIL}")
    print(f"Manager: {MANAGER_EMAIL}")
    print("=" * 50)

    credentials = get_credentials()
    admin_service = build("admin", "directory_v1", credentials=credentials)

    # Get direct reports
    print(f"\nFetching direct reports of {MANAGER_EMAIL}...")
    reports = get_direct_reports(admin_service, MANAGER_EMAIL)
    print(f"Found {len(reports)} direct reports:")
    for r in reports:
        print(f"  - {r.get('name', {}).get('fullName', '')} ({r['primaryEmail']})")

    if not reports:
        print("No direct reports found. Exiting.")
        return

    # Create group
    print(f"\nCreating group {GROUP_EMAIL}...")
    create_group(admin_service, GROUP_EMAIL, GROUP_NAME)

    # Add members
    print(f"\nAdding members to {GROUP_EMAIL}...")
    for report in reports:
        add_member_to_group(admin_service, GROUP_EMAIL, report["primaryEmail"])

    print(f"\nDone! Group {GROUP_EMAIL} created with {len(reports)} members.")


if __name__ == "__main__":
    main()
