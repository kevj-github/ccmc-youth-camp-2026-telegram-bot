import os
import json
import base64
import logging
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Sheet1")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SUPPORTED_VISION_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_creds = None
_sheets_service = None
_drive_service = None


# ── Credentials ───────────────────────────────────────────────────────
def _get_creds():
    global _creds
    if _creds is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            try:
                info = json.loads(creds_json)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}. "
                    "Make sure it's minified to a single line."
                )
            _creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        else:
            if not os.path.exists(SERVICE_ACCOUNT_FILE):
                raise RuntimeError(
                    f"No credentials found. Either set GOOGLE_CREDENTIALS_JSON env var "
                    f"or place service_account.json at {SERVICE_ACCOUNT_FILE}"
                )
            _creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds


def _sheets():
    global _sheets_service
    if _sheets_service is None:
        _sheets_service = build("sheets", "v4", credentials=_get_creds(), cache_discovery=False)
    return _sheets_service


def _drive():
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=_get_creds(), cache_discovery=False)
    return _drive_service


# ── Utility ───────────────────────────────────────────────────────────
def _quote_sheet_name(name: str) -> str:
    """Quote a sheet name if it has spaces or special chars."""
    if any(c in name for c in " '!"):
        return "'" + name.replace("'", "''") + "'"
    return name


def _col_num_to_letter(n: int) -> str:
    """Convert 1-indexed column number to letter (1 -> A, 27 -> AA)."""
    if n < 1:
        raise ValueError(f"Column number must be >= 1, got {n}")
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# ── Sheet operations ──────────────────────────────────────────────────
def read_sheet(range_: str = None) -> list[list[str]]:
    """Read rows from the sheet. Returns list of rows (each row is list of strings)."""
    range_name = range_ or _quote_sheet_name(SHEET_NAME)
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=range_name)
        .execute()
    )
    values = result.get("values", [])
    # Normalize: make sure everything is strings
    return [[str(cell) if cell is not None else "" for cell in row] for row in values]


def write_sheet(range_: str, values: list[list]) -> None:
    """Write values to a specific range in the sheet."""
    if not values:
        raise ValueError("Cannot write empty values")
    _sheets().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_,
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()


def append_sheet(values: list[list]) -> None:
    """Append rows to the end of the sheet."""
    if not values:
        raise ValueError("Cannot append empty values")
    _sheets().spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=_quote_sheet_name(SHEET_NAME),
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values}
    ).execute()


def add_column(header: str) -> str:
    """Add a new column with the given header at the end. Returns the column letter."""
    if not header or not header.strip():
        raise ValueError("Header cannot be empty")
    header = header.strip()

    # Get current header row to find how many columns exist
    header_row = read_sheet(f"{_quote_sheet_name(SHEET_NAME)}!1:1")
    current_cols = len(header_row[0]) if header_row and header_row[0] else 0
    new_col_letter = _col_num_to_letter(current_cols + 1)

    write_sheet(f"{_quote_sheet_name(SHEET_NAME)}!{new_col_letter}1", [[header]])
    return new_col_letter


# ── Drive operations ──────────────────────────────────────────────────
def list_drive_images() -> list[dict]:
    """List all image files in the configured Drive folder."""
    # Escape any single quotes in folder ID just in case
    folder_id = DRIVE_FOLDER_ID.replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents "
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
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return result.get("files", [])


def get_image_base64(file_id: str) -> tuple[str, str]:
    """Download a file from Drive and return (base64_string, mime_type)."""
    creds = _get_creds()

    # Get metadata
    file_meta = _drive().files().get(
        fileId=file_id,
        fields="mimeType, size, name",
        supportsAllDrives=True,
    ).execute()
    mime_type = file_meta.get("mimeType", "image/jpeg")
    size = int(file_meta.get("size", 0))

    # Guard against huge files (>20MB — Gemini will likely reject anyway)
    if size > 20 * 1024 * 1024:
        raise ValueError(
            f"Image '{file_meta.get('name')}' is too large ({size / 1024 / 1024:.1f}MB). "
            "Max supported: 20MB."
        )

    # Normalize unsupported mimes to jpeg
    if mime_type not in SUPPORTED_VISION_MIMES:
        logger.warning(f"Unsupported mime type {mime_type}, treating as image/jpeg")
        mime_type = "image/jpeg"

    # Download with fresh token
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=60,
    )
    response.raise_for_status()

    b64 = base64.b64encode(response.content).decode("utf-8")
    return b64, mime_type