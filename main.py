import json
import time
import tempfile
import os
import urllib.request
from datetime import datetime, timezone, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
from google.cloud import storage as gcs

import config


SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.datatransfer",
    "https://www.googleapis.com/auth/ediscovery",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/devstorage.read_only",
]


def send_slack_notification(message):
    """Send a notification to Slack via webhook."""
    if not config.SLACK_WEBHOOK_URL:
        return

    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        config.SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"  WARNING: Failed to send Slack notification: {e}")


def send_slack_user_summary(summary):
    """Send a per-user Slack notification with backup details."""
    if "error" in summary:
        msg = (
            f":x: *Offboarding FAILED* for `{summary['user_email']}`\n"
            f"Error: {summary['error']}"
        )
    else:
        exports = ", ".join(summary.get("exports_completed", []))
        failed = ", ".join(summary.get("exports_failed", []))
        deleted_status = ":white_check_mark: Account deleted" if summary.get("account_deleted") else ":pause_button: Account NOT deleted"

        msg = (
            f":white_check_mark: *Offboarding completed* for `{summary['user_email']}`\n"
            f"• *Name:* {summary.get('user_name', 'N/A')}\n"
            f"• *Data backed up:* {exports or 'None'}\n"
        )
        if failed:
            msg += f"• *Failed exports:* {failed}\n"
        msg += (
            f"• *Backup folder:* `{summary.get('backup_folder_id', 'N/A')}`\n"
            f"• *Vault matter:* `{summary.get('matter_id', 'N/A')}`\n"
            f"• *Status:* {deleted_status}\n"
            f"• *Time:* {summary.get('offboarded_at', 'N/A')}"
        )

    send_slack_notification(msg)


def get_credentials():
    """Create credentials from service account JSON."""
    if config.SERVICE_ACCOUNT_JSON:
        info = json.loads(config.SERVICE_ACCOUNT_JSON)
    else:
        with open("service_account.json", "r") as f:
            info = json.load(f)

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    # Impersonate the admin user for domain-wide delegation
    return credentials.with_subject(config.ADMIN_EMAIL)


def get_suspended_users(admin_service):
    """Get users suspended for more than the threshold days."""
    threshold_date = datetime.now(timezone.utc) - timedelta(
        days=config.SUSPENSION_THRESHOLD_DAYS
    )

    users = []
    page_token = None

    while True:
        results = (
            admin_service.users()
            .list(
                domain=config.DOMAIN,
                query="isSuspended=true",
                maxResults=100,
                pageToken=page_token,
                fields="nextPageToken,users(primaryEmail,suspensionReason,creationTime,lastLoginTime,name,id)",
            )
            .execute()
        )

        for user in results.get("users", []):
            last_login = user.get("lastLoginTime")
            if last_login:
                last_login_dt = datetime.fromisoformat(
                    last_login.replace("Z", "+00:00")
                )
                if last_login_dt < threshold_date:
                    users.append(user)

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return users


def create_vault_export(vault_service, user_email, matter_id, export_type):
    """Create a Vault export for a specific user and data type."""
    corpus_map = {
        "mail": "MAIL",
        "drive": "DRIVE",
        "calendar": "CALENDAR",
    }

    export_name = f"{user_email}_{export_type}_{datetime.now().strftime('%Y%m%d')}"

    export_body = {
        "name": export_name,
        "query": {
            "corpus": corpus_map[export_type],
            "dataScope": "ALL_DATA",
            "searchMethod": "ACCOUNT",
            "accountInfo": {"emails": [user_email]},
        },
        "exportOptions": {
            "region": "ANY",
        },
    }

    if export_type == "mail":
        export_body["exportOptions"]["mailOptions"] = {"exportFormat": "MBOX"}
    elif export_type == "drive":
        export_body["exportOptions"]["driveOptions"] = {
            "includeAccessInfo": False
        }

    export = (
        vault_service.matters()
        .exports()
        .create(matterId=matter_id, body=export_body)
        .execute()
    )

    return export


def wait_for_export(vault_service, matter_id, export_id, timeout_minutes=60):
    """Wait for a Vault export to complete."""
    deadline = time.time() + (timeout_minutes * 60)

    while time.time() < deadline:
        export = (
            vault_service.matters()
            .exports()
            .get(matterId=matter_id, exportId=export_id)
            .execute()
        )

        status = export.get("status")
        if status == "COMPLETED":
            return export
        elif status == "FAILED":
            raise Exception(
                f"Export {export_id} failed: {export.get('stats', {})}"
            )

        print(f"  Export {export_id} status: {status}, waiting...")
        time.sleep(30)

    raise Exception(f"Export {export_id} timed out after {timeout_minutes} minutes")


def stream_gcs_to_drive(gcs_client, drive_service, bucket_name, object_name, folder_id, file_name):
    """Stream a file from GCS directly to Drive using resumable upload with retries."""
    import io
    import ssl

    CHUNK_SIZE = 50 * 1024 * 1024  # 50MB chunks

    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.reload()
    file_size = blob.size

    print(f"    Streaming {file_name} ({file_size} bytes) from GCS to Drive...")

    file_metadata = {"name": file_name, "parents": [folder_id]}

    # Initiate resumable upload
    media = MediaIoBaseUpload(
        io.BytesIO(b""),  # placeholder, we'll use raw resumable
        mimetype="application/octet-stream",
        resumable=True,
    )

    # For small files (<50MB), download and upload directly
    if file_size < CHUNK_SIZE:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                content = blob.download_as_bytes()
                media = MediaIoBaseUpload(
                    io.BytesIO(content),
                    mimetype="application/octet-stream",
                    resumable=True,
                    chunksize=CHUNK_SIZE,
                )
                uploaded = drive_service.files().create(
                    body=file_metadata, media_body=media, fields="id,name,size"
                ).execute()
                print(f"    Uploaded {file_name} to Drive ({uploaded.get('size')} bytes)")
                return uploaded
            except (ssl.SSLError, Exception) as e:
                if attempt < max_retries - 1:
                    print(f"    Retry {attempt + 1}/{max_retries} for {file_name}: {e}")
                    time.sleep(5 * (attempt + 1))
                else:
                    raise

    # For large files, stream in chunks using a temporary pipe approach
    # Download from GCS in chunks, upload to Drive via resumable upload
    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file_name}") as tmp:
        tmp_path = tmp.name

    try:
        # Download with retries
        max_retries = 3
        for attempt in range(max_retries):
            try:
                blob.download_to_filename(tmp_path)
                break
            except (ssl.SSLError, Exception) as e:
                if attempt < max_retries - 1:
                    print(f"    GCS download retry {attempt + 1}/{max_retries}: {e}")
                    time.sleep(5 * (attempt + 1))
                else:
                    raise

        # Upload with retries and chunking
        media = MediaFileUpload(tmp_path, resumable=True, chunksize=CHUNK_SIZE)
        request = drive_service.files().create(
            body=file_metadata, media_body=media, fields="id,name,size"
        )

        response = None
        while response is None:
            for attempt in range(max_retries):
                try:
                    status, response = request.next_chunk()
                    if status:
                        print(f"    Upload progress: {int(status.progress() * 100)}%")
                    break
                except (ssl.SSLError, Exception) as e:
                    if attempt < max_retries - 1:
                        print(f"    Upload chunk retry {attempt + 1}/{max_retries}: {e}")
                        time.sleep(5 * (attempt + 1))
                    else:
                        raise

        print(f"    Uploaded {file_name} to Drive ({response.get('size')} bytes)")
        return response
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def process_export_files(vault_service, drive_service, matter_id, export_id, folder_id, credentials):
    """Download export files from Vault GCS and upload to Drive with streaming."""
    export = (
        vault_service.matters()
        .exports()
        .get(matterId=matter_id, exportId=export_id)
        .execute()
    )

    cloud_storage_sink = export.get("cloudStorageSink", {})
    files = cloud_storage_sink.get("files", [])
    gcs_client = gcs.Client(credentials=credentials)

    uploaded_files = []
    for i, file_info in enumerate(files):
        bucket_name = file_info.get("bucketName")
        object_name = file_info.get("objectName")

        if not bucket_name or not object_name:
            continue

        file_name = os.path.basename(object_name) or f"export_part_{i}"
        result = stream_gcs_to_drive(
            gcs_client, drive_service, bucket_name, object_name, folder_id, file_name
        )
        uploaded_files.append({"file_name": file_name, "drive_id": result.get("id")})

    return uploaded_files


def create_user_drive_folder(drive_service, user_email):
    """Create a folder for the user in the backup Drive folder."""
    folder_name = f"{user_email}_backup_{datetime.now().strftime('%Y%m%d')}"

    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [config.BACKUP_FOLDER_ID],
    }

    folder = (
        drive_service.files()
        .create(body=folder_metadata, fields="id,webViewLink")
        .execute()
    )

    return folder


def upload_to_drive(drive_service, folder_id, file_path, file_name):
    """Upload a file to Google Drive."""
    file_metadata = {
        "name": file_name,
        "parents": [folder_id],
    }

    media = MediaFileUpload(file_path, resumable=True)
    uploaded_file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id,name,size")
        .execute()
    )

    return uploaded_file


def transfer_drive_ownership(datatransfer_service, admin_service, user_id):
    """Transfer user's Drive files to svc-super@devrev.ai using Data Transfer API."""
    target_user = admin_service.users().get(userKey=config.TRANSFER_TO_EMAIL, fields="id").execute()
    target_user_id = target_user["id"]

    # Google Drive app ID is 55656082996
    transfer_body = {
        "oldOwnerUserId": user_id,
        "newOwnerUserId": target_user_id,
        "applicationDataTransfers": [
            {
                "applicationId": "55656082996",
                "applicationTransferParams": [
                    {"key": "PRIVACY_LEVEL", "value": ["SHARED", "PRIVATE"]}
                ],
            }
        ],
    }

    transfer = datatransfer_service.transfers().insert(body=transfer_body).execute()
    print(f"  Drive ownership transfer initiated: {transfer.get('id')}")
    return transfer


def delete_user(admin_service, user_email):
    """Delete a user account from Google Workspace."""
    if not config.DELETE_AFTER_BACKUP:
        print(f"  [DRY RUN] Would delete user: {user_email}")
        return False

    admin_service.users().delete(userKey=user_email).execute()
    print(f"  DELETED user: {user_email}")
    return True


def process_user(user, vault_service, drive_service, admin_service, datatransfer_service):
    """Process a single user: export data, transfer ownership, upload to Drive, delete account."""
    user_email = user["primaryEmail"]
    user_name = user.get("name", {}).get("fullName", user_email)
    print(f"\nProcessing: {user_name} ({user_email})")

    # Step 1: Create a Vault matter for this user's exports
    matter_body = {
        "name": f"Offboarding - {user_email} - {datetime.now().strftime('%Y-%m-%d')}",
        "description": f"Automated offboarding backup for {user_email}",
    }
    matter = vault_service.matters().create(body=matter_body).execute()
    matter_id = matter["matterId"]
    print(f"  Created Vault matter: {matter_id}")

    # Step 2: Create a Drive folder for this user
    user_folder = create_user_drive_folder(drive_service, user_email)
    folder_id = user_folder["id"]
    print(f"  Created backup folder: {user_folder.get('webViewLink', folder_id)}")

    # Step 3: Export Gmail and Drive data
    export_types = ["mail", "drive"]
    exports = {}

    for export_type in export_types:
        try:
            export = create_vault_export(
                vault_service, user_email, matter_id, export_type
            )
            exports[export_type] = export
            print(f"  Started {export_type} export: {export['id']}")
        except Exception as e:
            print(f"  WARNING: Failed to start {export_type} export: {e}")

    # Step 4: Wait for all exports to complete
    completed_exports = {}
    for export_type, export in exports.items():
        try:
            completed = wait_for_export(vault_service, matter_id, export["id"])
            completed_exports[export_type] = completed
            print(f"  {export_type} export completed")
        except Exception as e:
            print(f"  WARNING: {export_type} export failed: {e}")

    # Step 5: Stream export files from Vault Cloud Storage directly to Drive
    credentials = get_credentials()
    uploaded_exports = []
    failed_uploads = []
    for export_type, export in completed_exports.items():
        try:
            uploaded_files = process_export_files(
                vault_service, drive_service, matter_id, export["id"], folder_id, credentials
            )
            if not uploaded_files:
                raise Exception("No files transferred from Cloud Storage to Drive")

            uploaded_exports.append(export_type)

            # Upload metadata as reference
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(
                    {
                        "export_type": export_type,
                        "user": user_email,
                        "export_id": export["id"],
                        "matter_id": matter_id,
                        "status": "COMPLETED",
                        "stats": export.get("stats", {}),
                        "files_exported": len(uploaded_files),
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                    },
                    f,
                    indent=2,
                )
                info_path = f.name
            upload_to_drive(drive_service, folder_id, info_path, f"{export_type}_export_info.json")
            os.unlink(info_path)
            print(f"  Uploaded {export_type} export info to Drive")
        except Exception as e:
            print(f"  WARNING: Failed to transfer {export_type} files: {e}")
            failed_uploads.append(export_type)

    # Step 6: Close the Vault matter (set to CLOSED state for retention)
    for attempt in range(3):
        try:
            vault_service.matters().close(matterId=matter_id, body={}).execute()
            print(f"  Closed Vault matter (data retained)")
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Vault close retry {attempt + 1}/3: {e}")
                time.sleep(5 * (attempt + 1))
            else:
                print(f"  WARNING: Could not close Vault matter: {e}")

    # Step 7: Transfer Drive ownership and delete user account
    # Only proceed if ALL exports were actually downloaded and uploaded to Drive
    account_deleted = False
    all_data_saved = len(uploaded_exports) == len(export_types) and not failed_uploads

    if config.DELETE_AFTER_BACKUP and all_data_saved:
        try:
            transfer_drive_ownership(datatransfer_service, admin_service, user["id"])
            print(f"  Drive ownership transferred to {config.TRANSFER_TO_EMAIL}")
        except Exception as e:
            print(f"  WARNING: Drive transfer failed: {e}")

        delete_user(admin_service, user_email)
        account_deleted = True
    elif not all_data_saved:
        print(
            f"  SKIPPING deletion - not all exports were saved to Drive "
            f"(uploaded: {uploaded_exports}, failed: {failed_uploads})"
        )
    else:
        print(f"  [DRY RUN] Skipping deletion")

    # Step 8: Create a summary file
    summary = {
        "user_email": user_email,
        "user_name": user_name,
        "offboarded_at": datetime.now(timezone.utc).isoformat(),
        "matter_id": matter_id,
        "backup_folder_id": folder_id,
        "exports_completed": uploaded_exports,
        "exports_failed": failed_uploads + [t for t in export_types if t not in completed_exports],
        "account_deleted": account_deleted,
    }

    summary_path = os.path.join(tempfile.gettempdir(), "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    for attempt in range(3):
        try:
            upload_to_drive(drive_service, folder_id, summary_path, "offboarding_summary.json")
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"  WARNING: Could not upload summary: {e}")

    return summary


def main():
    print("=" * 60)
    print("Google Workspace Offboarding - Automated Backup & Cleanup")
    print(f"Run time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Domain: {config.DOMAIN}")
    print(f"Suspension threshold: {config.SUSPENSION_THRESHOLD_DAYS} days")
    print(f"Delete after backup: {config.DELETE_AFTER_BACKUP}")
    print("=" * 60)

    credentials = get_credentials()

    admin_service = build("admin", "directory_v1", credentials=credentials)
    datatransfer_service = build("admin", "datatransfer_v1", credentials=credentials)
    vault_service = build("vault", "v1", credentials=credentials)
    drive_service = build("drive", "v3", credentials=credentials)

    print("\nSearching for suspended users...")
    suspended_users = get_suspended_users(admin_service)
    print(f"Found {len(suspended_users)} users suspended for 45+ days")

    if config.TEST_USER:
        suspended_users = [u for u in suspended_users if u["primaryEmail"] == config.TEST_USER]
        if not suspended_users:
            print(f"\nTEST_USER '{config.TEST_USER}' not found in suspended users list.")
            return
        print(f"\n[TEST MODE] Only processing: {config.TEST_USER}")

    if not suspended_users:
        print("\nNo users to process. Done.")
        send_slack_notification(
            ":information_source: *Offboarding run complete* — no users to process (0 suspended 45+ days)."
        )
        return

    send_slack_notification(
        f":rocket: *Offboarding started* — processing {len(suspended_users)} user(s) suspended 45+ days."
    )

    results = []
    for user in suspended_users:
        try:
            summary = process_user(user, vault_service, drive_service, admin_service, datatransfer_service)
            results.append(summary)
            send_slack_user_summary(summary)
        except Exception as e:
            print(f"\nERROR processing {user['primaryEmail']}: {e}")
            error_result = {"user_email": user["primaryEmail"], "error": str(e)}
            results.append(error_result)
            send_slack_user_summary(error_result)

    successful = len([r for r in results if "error" not in r])
    failed = len([r for r in results if "error" in r])

    final_msg = (
        f":checkered_flag: *Offboarding run complete*\n"
        f"• *Total processed:* {len(results)}\n"
        f"• *Successful:* {successful}\n"
        f"• *Failed:* {failed}"
    )
    send_slack_notification(final_msg)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total processed: {len(results)}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()
