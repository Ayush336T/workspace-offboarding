# Google Workspace Offboarding Automation

Automatically backs up and deletes Google Workspace user accounts that have been suspended for 45+ days.

## What it does

1. Queries Google Admin SDK for suspended users (inactive 45+ days)
2. Creates a Google Vault export per user (Gmail, Drive, Calendar)
3. Stores export metadata in a per-user folder on Google Drive
4. Closes the Vault matter (retaining data permanently)
5. Deletes the user account after successful backup

## Setup

### 1. Create a Google Cloud Project

1. Go to https://console.cloud.google.com
2. Create a new project (e.g., "workspace-offboarding")
3. Enable these APIs:
   - Admin SDK API
   - Google Vault API
   - Google Drive API

### 2. Create a Service Account

1. In your GCP project, go to **IAM & Admin > Service Accounts**
2. Click **Create Service Account**
3. Name it (e.g., "offboarding-bot")
4. Click **Create and Continue** (skip optional permissions)
5. Click **Done**
6. Click on the service account you just created
7. Go to **Keys** tab > **Add Key** > **Create new key** > **JSON**
8. Download the JSON file (you'll need its contents later)

### 3. Enable Domain-Wide Delegation

1. On the service account page, click **Edit** (pencil icon)
2. Check **Enable Google Workspace Domain-wide Delegation**
3. Save
4. Note the **Client ID** (a number like 1234567890)

### 4. Authorize in Google Workspace Admin

1. Go to https://admin.google.com
2. Navigate to **Security > Access and data control > API controls**
3. Click **Manage Domain Wide Delegation**
4. Click **Add new**
5. Enter the Client ID from step 3
6. Add these OAuth scopes (comma-separated):
   ```
   https://www.googleapis.com/auth/admin.directory.user,https://www.googleapis.com/auth/ediscovery,https://www.googleapis.com/auth/drive
   ```
7. Click **Authorize**

### 5. Create GitHub Repository

1. Create a new private repository on GitHub
2. Push this code to it

### 6. Configure GitHub Secrets

Go to your repo > **Settings > Secrets and variables > Actions** and add:

| Secret Name | Value |
|---|---|
| `GOOGLE_DOMAIN` | Your domain (e.g., `company.com`) |
| `ADMIN_EMAIL` | A super admin email (e.g., `admin@company.com`) |
| `BACKUP_FOLDER_ID` | `16eM5mxtkZCppNmB1gYz656tZhD4Z-i2F` |
| `SERVICE_ACCOUNT_JSON` | The entire contents of the JSON key file |

### 7. Done!

The workflow runs daily at 2:00 AM UTC. You can also trigger it manually from the Actions tab.

## Dry Run Mode

To test without deleting accounts, change `DELETE_AFTER_BACKUP` to `"false"` in the workflow file.

## Data Retention

- Vault matters are closed (not deleted) — data is retained permanently
- Export metadata is stored in Google Drive indefinitely
- User backup folders are never auto-deleted
