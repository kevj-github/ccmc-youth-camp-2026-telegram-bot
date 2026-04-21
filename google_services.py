import os
import base64
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Sheet1")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Mime types we support for vision
IMAGE_MIME_MAP = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}

_creds = None
_sheets_service = None
_drive_service = None


def _get_creds():
    global _creds
    if _creds is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            # Render: credentials passed as env var
            import json
            info = json.loads(creds_json)
            _creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        else:
            # Local: credentials from file
            _creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds


def _sheets():
    global _sheets_service
    if _sheets_service is None:
        _sheets_service = build("sheets", "v4", credentials=_get_creds())
    return _sheets_service


def _drive():
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=_get_creds())
    return _drive_service


def read_sheet(range_: str = None) -> list[list[str]]:
    """Read all data from the sheet. Returns list of rows."""
    range_name = range_ or SHEET_NAME
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=range_name)
        .execute()
    )
    return result.get("values", [])


def write_sheet(range_: str, values: list[list]) -> None:
    """Write values to a specific range in the sheet."""
    _sheets().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_,
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()


def append_sheet(values: list[list]) -> None:
    """Append rows to the end of the sheet."""
    _sheets().spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_NAME,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values}
    ).execute()


def list_drive_images() -> list[dict]:
    """List all image files in the configured Drive folder."""
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and mimeType contains 'image/' "
        f"and trashed = false"
    )
    result = (
        _drive()
        .files()
        .list(
            q=query,
            fields="files(id, name, mimeType, createdTime)",
            orderBy="createdTime desc",
            pageSize=50
        )
        .execute()
    )
    return result.get("files", [])


def get_image_base64(file_id: str) -> tuple[str, str]:
    """
    Download a file from Drive and return (base64_string, mime_type).
    Refreshes credentials before downloading.
    """
    creds = _get_creds()

    # Get file metadata to determine mime type
    file_meta = _drive().files().get(fileId=file_id, fields="mimeType").execute()
    mime_type = file_meta.get("mimeType", "image/jpeg")

    # Normalize mime type for Claude vision
    if mime_type not in IMAGE_MIME_MAP:
        mime_type = "image/jpeg"

    # Download the file content
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30
    )
    response.raise_for_status()

    b64 = base64.b64encode(response.content).decode("utf-8")
    return b64, mime_type