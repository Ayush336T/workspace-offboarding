import os

# Google Workspace domain
DOMAIN = os.environ.get("GOOGLE_DOMAIN", "yourdomain.com")

# Admin email (a super admin account the service account impersonates)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@yourdomain.com")

# Google Drive folder ID for backups
# Extracted from: https://drive.google.com/drive/folders/16eM5mxtkZCppNmB1gYz656tZhD4Z-i2F
BACKUP_FOLDER_ID = os.environ.get("BACKUP_FOLDER_ID", "16eM5mxtkZCppNmB1gYz656tZhD4Z-i2F")

# Number of days a user must be suspended before offboarding
SUSPENSION_THRESHOLD_DAYS = int(os.environ.get("SUSPENSION_THRESHOLD_DAYS", "45"))

# Whether to actually delete user accounts after backup (set to False for dry runs)
DELETE_AFTER_BACKUP = os.environ.get("DELETE_AFTER_BACKUP", "true").lower() == "true"

# Service account credentials JSON (passed as env var in GitHub Actions)
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")
