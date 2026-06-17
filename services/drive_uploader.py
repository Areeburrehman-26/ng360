# google_drive_uploader.py → PDF upload & folders
"""
drive_uploader.py
-----------------
Google Drive API v3 client for the NG360 Bot.

Uses OAuth user credentials (same oauth_token.json as the HOA bot) — NOT a
service account, because service accounts have no storage quota and can't own
files in personal Drives.

Drive Folder Structure:
  Trustwell Insurance Quotes/
    John Doe/
            NG360_Quote_20260310_143045.pdf
    Jane Smith/
            NG360_Quote_20260309_165432.pdf

RULE: Always check for an existing folder before creating to avoid duplicates.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OAUTH_TOKEN_PATH = os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")
DRIVE_SCOPES     = ["https://www.googleapis.com/auth/drive"]
PDF_MIME_TYPE    = "application/pdf"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    """Raised when a Google Drive operation fails."""


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------

def _build_service():
    """
    Build and return an authenticated Google Drive service object using
    OAuth user credentials (NOT a service account).

    Requires a token file produced by hoa_bot_rebuild/scripts/auth_drive.py.
    Token path is set via GOOGLE_OAUTH_TOKEN_PATH in .env.
    """
    if not OAUTH_TOKEN_PATH:
        raise DriveError("GOOGLE_OAUTH_TOKEN_PATH is not set in .env")

    token_path = Path(OAUTH_TOKEN_PATH)
    if not token_path.exists():
        raise DriveError(
            f"OAuth token not found at {token_path}. "
            "Run: python3 hoa_bot_rebuild/scripts/auth_drive.py"
        )

    creds = UserCredentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            raise DriveError(
                "OAuth credentials are invalid and cannot be refreshed. "
                "Re-run: python3 hoa_bot_rebuild/scripts/auth_drive.py"
            )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Folder helpers
# ---------------------------------------------------------------------------

def _find_folder(service, name: str, parent_id: str | None = None) -> str | None:
    """
    Search for a folder by name under an optional parent.
    Returns the folder ID if found, else None.
    """
    query_parts = [
        f"name = '{name}'",
        f"mimeType = '{FOLDER_MIME_TYPE}'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")

    query = " and ".join(query_parts)
    response = service.files().list(
        q=query,
        fields="files(id, name)",
        spaces="drive",
    ).execute()

    files = response.get("files", [])
    if files:
        logger.debug("[drive_uploader] Found existing folder '%s' (id=%s)", name, files[0]["id"])
        return files[0]["id"]
    return None


def _create_folder(service, name: str, parent_id: str | None = None) -> str:
    """
    Create a folder with the given name under an optional parent.
    Returns the new folder ID.
    """
    metadata = {
        "name": name,
        "mimeType": FOLDER_MIME_TYPE,
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    logger.info("[drive_uploader] Created folder '%s' (id=%s)", name, folder_id)
    return folder_id


def _get_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Find an existing folder or create it if absent. Returns the folder ID."""
    existing_id = _find_folder(service, name, parent_id)
    if existing_id:
        return existing_id
    return _create_folder(service, name, parent_id)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def _make_shareable(service, file_id: str) -> None:
    """Grant 'anyone with the link can view' permission to a file."""
    permission = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=file_id, body=permission).execute()
    logger.debug("[drive_uploader] Set shareable permission on file %s", file_id)


def _get_shareable_url(file_id: str) -> str:
    """Construct the shareable Google Drive URL for a file ID."""
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_quote_pdf(
    pdf_path: str | Path,
    first_name: str,
    last_name: str,
) -> str:
    """
    Upload a quote PDF to Google Drive in the correct customer folder.

    Steps:
      1. Place directly inside the GDRIVE_FOLDER_ID root folder
      2. Find or create "{first_name} {last_name}" subfolder
      3. Upload PDF with timestamped filename
      4. Set shareable permissions
      5. Return shareable URL

    Args:
        pdf_path:   Local path to the PDF file.
        first_name: Customer first name (used for folder name).
        last_name:  Customer last name (used for folder name).

    Returns:
        Shareable Google Drive URL string.

    Raises:
        DriveError: on any Drive API failure.
        FileNotFoundError: if pdf_path does not exist.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found at: {pdf_path}")

    if not GDRIVE_FOLDER_ID:
        raise DriveError("GDRIVE_FOLDER_ID is not set in .env")

    customer_name = f"{first_name.strip()} {last_name.strip()}"
    filename = _timestamped_filename()

    logger.info(
        "[drive_uploader] Uploading '%s' for customer '%s'", filename, customer_name
    )

    try:
        service = _build_service()

        # Create the customer subfolder directly inside the configured NG360 folder
        customer_id = _get_or_create_folder(service, customer_name, parent_id=GDRIVE_FOLDER_ID)

        file_metadata = {
            "name": filename,
            "parents": [customer_id],
        }
        media = MediaFileUpload(str(pdf_path), mimetype=PDF_MIME_TYPE, resumable=False)
        uploaded = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        ).execute()

        file_id = uploaded["id"]
        _make_shareable(service, file_id)
        url = _get_shareable_url(file_id)

        logger.info("[drive_uploader] Upload complete: %s", url)
        return url

    except HttpError as exc:
        raise DriveError(f"Google Drive API error: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _timestamped_filename() -> str:
    """Return a PDF filename with UTC timestamp, e.g. NG360_Quote_20260310_143045.pdf"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"NG360_Quote_{ts}.pdf"
