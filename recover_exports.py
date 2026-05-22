import json
import os
import tempfile

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.cloud import storage as gcs

import config

SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/ediscovery",
    "https://www.googleapis.com/auth/drive",
]


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


def list_backup_subfolders(drive_service):
    """List all subfolders in the backup folder."""
    folders = []
    page_token = None
    while True:
        results = drive_service.files().list(
            q=f"'{config.BACKUP_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken,files(id,name)",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        folders.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return folders


def list_folder_contents(drive_service, folder_id):
    """List files in a folder."""
    files = []
    page_token = None
    while True:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,size,mimeType)",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files


def download_drive_file(drive_service, file_id, dest_path):
    """Download a file from Drive."""
    request = drive_service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as f:
        downloader = MediaFileUpload  # unused, just for import consistency
        import io
        from googleapiclient.http import MediaIoBaseDownload
        fh = io.FileIO(dest_path, "wb")
        dl = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = dl.next_chunk()


def upload_to_drive(drive_service, folder_id, file_path, file_name):
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, resumable=True)
    return drive_service.files().create(
        body=file_metadata, media_body=media, fields="id,name,size"
    ).execute()


def delete_folder(drive_service, folder_id, folder_name):
    """Permanently delete a folder."""
    drive_service.files().delete(fileId=folder_id).execute()
    print(f"  DELETED empty folder: {folder_name}")


def create_fresh_export(vault_service, matter_id, user_email, export_type):
    """Create a new Vault export for a user."""
    from datetime import datetime
    corpus_map = {"mail": "MAIL", "drive": "DRIVE"}
    export_name = f"{user_email}_{export_type}_recovery_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    export_body = {
        "name": export_name,
        "query": {
            "corpus": corpus_map[export_type],
            "dataScope": "ALL_DATA",
            "searchMethod": "ACCOUNT",
            "accountInfo": {"emails": [user_email]},
        },
        "exportOptions": {"region": "ANY"},
    }

    if export_type == "mail":
        export_body["exportOptions"]["mailOptions"] = {"exportFormat": "MBOX"}
    elif export_type == "drive":
        export_body["exportOptions"]["driveOptions"] = {"includeAccessInfo": False}

    return vault_service.matters().exports().create(
        matterId=matter_id, body=export_body
    ).execute()


def wait_for_export(vault_service, matter_id, export_id, timeout_minutes=60):
    """Wait for a Vault export to complete."""
    import time
    deadline = time.time() + (timeout_minutes * 60)

    while time.time() < deadline:
        export = vault_service.matters().exports().get(
            matterId=matter_id, exportId=export_id
        ).execute()

        status = export.get("status")
        if status == "COMPLETED":
            return export
        elif status == "FAILED":
            raise Exception(f"Export failed: {export.get('stats', {})}")

        time.sleep(30)

    raise Exception(f"Export timed out after {timeout_minutes} minutes")


def download_export_from_gcs(gcs_client, export, temp_dir):
    """Download export files from Cloud Storage. Returns list of local file info."""
    cloud_storage_sink = export.get("cloudStorageSink", {})
    files = cloud_storage_sink.get("files", [])
    downloaded = []

    for i, file_info in enumerate(files):
        bucket_name = file_info.get("bucketName")
        object_name = file_info.get("objectName")
        if not bucket_name or not object_name:
            continue

        file_name = os.path.basename(object_name)
        local_path = os.path.join(temp_dir, file_name or f"export_{i}")

        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.download_to_filename(local_path)
        downloaded.append({"local_path": local_path, "file_name": file_name})

    return downloaded


def recover_from_vault(vault_service, drive_service, gcs_client, folder_id, summary):
    """Re-download Vault exports and upload to the user's backup folder."""
    matter_id = summary.get("matter_id")
    user_email = summary.get("user_email")
    if not matter_id:
        print("    No matter_id in summary, cannot recover")
        return False

    # Re-open the matter if it's closed
    try:
        matter = vault_service.matters().get(matterId=matter_id).execute()
        if matter.get("state") == "CLOSED":
            vault_service.matters().reopen(matterId=matter_id, body={}).execute()
            print(f"    Re-opened Vault matter: {matter_id}")
    except Exception as e:
        print(f"    Cannot access Vault matter {matter_id}: {e}")
        return False

    # List existing exports in this matter
    exports = vault_service.matters().exports().list(matterId=matter_id).execute()
    export_list = exports.get("exports", [])

    recovered = False
    with tempfile.TemporaryDirectory() as temp_dir:
        # First, try to download from existing completed exports
        for export in export_list:
            if export.get("status") != "COMPLETED":
                continue

            try:
                downloaded = download_export_from_gcs(gcs_client, export, temp_dir)
                for file_info in downloaded:
                    upload_to_drive(drive_service, folder_id, file_info["local_path"], file_info["file_name"])
                    print(f"    Recovered: {file_info['file_name']} ({os.path.getsize(file_info['local_path'])} bytes)")
                    recovered = True
            except Exception as e:
                print(f"    Existing export download failed (likely expired): {e}")

        # If existing exports couldn't be downloaded, create fresh exports
        if not recovered and user_email:
            print(f"    Existing exports expired, creating fresh exports for {user_email}...")
            for export_type in ["mail", "drive"]:
                try:
                    new_export = create_fresh_export(vault_service, matter_id, user_email, export_type)
                    print(f"    Started fresh {export_type} export: {new_export['id']}")
                    completed_export = wait_for_export(vault_service, matter_id, new_export["id"])
                    downloaded = download_export_from_gcs(gcs_client, completed_export, temp_dir)
                    for file_info in downloaded:
                        upload_to_drive(drive_service, folder_id, file_info["local_path"], file_info["file_name"])
                        print(f"    Recovered: {file_info['file_name']} ({os.path.getsize(file_info['local_path'])} bytes)")
                        recovered = True
                except Exception as e:
                    print(f"    Fresh {export_type} export failed: {e}")

    # Re-close the matter
    vault_service.matters().close(matterId=matter_id, body={}).execute()
    return recovered


def main():
    print("=" * 60)
    print("Recovery Script - Re-download Vault exports & clean up empty folders")
    print("=" * 60)

    credentials = get_credentials()
    drive_service = build("drive", "v3", credentials=credentials)
    vault_service = build("vault", "v1", credentials=credentials)
    gcs_client = gcs.Client(credentials=credentials)

    print("\nScanning backup folder for subfolders...")
    folders = list_backup_subfolders(drive_service)
    print(f"Found {len(folders)} user backup folders\n")

    empty_deleted = 0
    recovered = 0
    skipped = 0

    for folder in folders:
        folder_id = folder["id"]
        folder_name = folder["name"]
        print(f"Checking: {folder_name}")

        contents = list_folder_contents(drive_service, folder_id)

        # Completely empty folder — delete it
        if not contents:
            delete_folder(drive_service, folder_id, folder_name)
            empty_deleted += 1
            continue

        # Check if folder only has metadata files (no actual export data)
        file_names = [f["name"] for f in contents]
        metadata_only = all(
            name.endswith("_export_info.json") or name == "offboarding_summary.json"
            for name in file_names
        )

        if not metadata_only:
            print(f"  Already has export data ({len(contents)} files), skipping")
            skipped += 1
            continue

        # Try to recover from Vault using the summary
        summary_file = next((f for f in contents if f["name"] == "offboarding_summary.json"), None)
        if not summary_file:
            print(f"  Metadata-only but no summary file, cannot recover — deleting")
            delete_folder(drive_service, folder_id, folder_name)
            empty_deleted += 1
            continue

        # Download the summary to get matter_id
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        download_drive_file(drive_service, summary_file["id"], tmp_path)
        with open(tmp_path, "r") as f:
            summary = json.load(f)
        os.unlink(tmp_path)

        print(f"  Recovering exports for {summary.get('user_email', 'unknown')}...")
        success = recover_from_vault(vault_service, drive_service, gcs_client, folder_id, summary)
        if success:
            recovered += 1
        else:
            print(f"  Could not recover — exports may have expired")
            skipped += 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Empty folders deleted: {empty_deleted}")
    print(f"  Folders recovered: {recovered}")
    print(f"  Folders skipped (already OK or unrecoverable): {skipped}")
    print("=" * 60)


if __name__ == "__main__":
    main()
