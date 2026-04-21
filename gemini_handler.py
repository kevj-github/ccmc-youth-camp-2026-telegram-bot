import os
import json
import base64
import io
import logging
import time
import re

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPIError
from google.generativeai.types import FunctionDeclaration, Tool
from googleapiclient.errors import HttpError
import PIL.Image

from google_services import (
    read_sheet, write_sheet, append_sheet,
    list_drive_images, get_image_base64,
    add_column, SHEET_NAME,
)

logger = logging.getLogger(__name__)

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# Use lite for higher free-tier rate limits (15 RPM vs 5 RPM on flash)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = f"""You are a helpful assistant managing a youth camp.
You have access to the camp's Google Sheet (registration data from a Google Form) and the linked Google Drive folder (PayNow payment receipts).

Sheet tab name: '{SHEET_NAME}'

The sheet has these columns (from the Google Form):
- Timestamp
- Full Name (e.g., Kevin Saputra)
- Preferred Name (e.g., Kevin)
- Age
- Phone Number
- Emergency Contact Number
- Preferred Language
- Dietary Restriction/Allergy
- Youth Camp T-Shirt Size
- Preferred Payment Method (e.g. Paynow full, Paynow partial)
- Payment Proof URL(s) — Google Drive link(s) to the PayNow screenshot. Multiple uploads separated by ", "

Important about payment proofs:
- The "Payment Proof" column contains Google Drive URLs (like https://drive.google.com/open?id=ABC123), NOT filenames.
- A camper may upload MULTIPLE images — their cell will contain multiple URLs separated by ", ".
- To analyze a camper's payment: read_sheet, find the camper by name, then call analyze_payment_proof with the ENTIRE contents of their payment proof cell (all URLs at once, don't split them yourself). The tool will analyze each image and sum the amounts automatically.

Guidelines:
- Keep replies concise and friendly — this is a Telegram chat
- The first row of the sheet is always the header
- When editing cells, call read_sheet first to know column positions/row numbers
- When adding a new column, use add_column — don't manually write to an empty cell
- When asked about someone's payment, search by their name in the sheet, grab their payment proof URL(s), and analyze each
- If analyze_payment_proof says "not a valid PayNow receipt", relay that verbatim — do NOT guess the amount
- Do NOT use markdown formatting (*bold*, _italic_) — plain text only
- When reporting amounts, include the camper's name and which row they're in"""

# ── Tool definitions ──────────────────────────────────────────────────
read_sheet_fn = FunctionDeclaration(
    name="read_sheet",
    description="Read all rows from the camp registration Google Sheet. Returns all data including headers.",
    parameters={"type": "object", "properties": {}}
)

write_sheet_fn = FunctionDeclaration(
    name="write_sheet",
    description=(
        "Write or update specific cells in the Google Sheet. "
        "Use this for updating existing cells. For adding new columns, use add_column instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "range": {
                "type": "string",
                "description": f"Cell range in A1 notation, e.g. '{SHEET_NAME}!B2' or '{SHEET_NAME}!C2:C5'"
            },
            "values": {
                "type": "array",
                "description": "2D array of string values. Each inner array is a row.",
                "items": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        "required": ["range", "values"]
    }
)

append_row_fn = FunctionDeclaration(
    name="append_row",
    description="Append a new row to the end of the sheet. Use this when adding a new camper registration manually.",
    parameters={
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "description": "A single row of string values to append.",
                "items": {"type": "string"}
            }
        },
        "required": ["values"]
    }
)

list_images_fn = FunctionDeclaration(
    name="list_images",
    description="List all image files uploaded to the Google Drive folder linked to the form.",
    parameters={"type": "object", "properties": {}}
)

analyze_payment_proof_fn = FunctionDeclaration(
    name="analyze_payment_proof",
    description=(
        "Analyze PayNow payment screenshot(s) from the 'Payment Proof' column of the sheet. "
        "Handles both single URLs and multiple URLs separated by ', ' (when a camper uploaded "
        "multiple images). Returns per-image breakdown AND the total amount paid across all images."
    ),
    parameters={
        "type": "object",
        "properties": {
            "drive_urls_or_ids": {
                "type": "string",
                "description": (
                    "The full content of the payment proof cell — can be a single Drive URL, "
                    "a raw file ID, or multiple URLs separated by ', '. "
                    "Copy the cell contents directly from the sheet."
                )
            },
            "camper_name": {
                "type": "string",
                "description": "Optional — the camper's name, just for context in the reply."
            }
        },
        "required": ["drive_urls_or_ids"]
    }
)

add_column_fn = FunctionDeclaration(
    name="add_column",
    description="Add a new column with a header to the spreadsheet. Use this when asked to add a new field/column like 'Fees paid', 'Notes', 'Attendance' etc.",
    parameters={
        "type": "object",
        "properties": {
            "header": {
                "type": "string",
                "description": "The column header text, e.g. 'Fees paid so far'"
            }
        },
        "required": ["header"]
    }
)

camp_tools = Tool(function_declarations=[
    read_sheet_fn,
    write_sheet_fn,
    append_row_fn,
    list_images_fn,
    analyze_payment_proof_fn,
    add_column_fn,
])

model = genai.GenerativeModel(
    model_name=GEMINI_MODEL,
    system_instruction=SYSTEM_PROMPT,
    tools=[camp_tools]
)


# ── Helpers ───────────────────────────────────────────────────────────
def _proto_to_native(value):
    """Recursively convert protobuf RepeatedComposite / MapComposite to native Python types."""
    if hasattr(value, "items") and callable(getattr(value, "items", None)):
        try:
            return {k: _proto_to_native(v) for k, v in value.items()}
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict)):
        try:
            return [_proto_to_native(v) for v in value]
        except Exception:
            pass
    return value


def _stringify_values(values):
    """Ensure all values in a 2D array are strings (Sheets API accepts strings best)."""
    if not isinstance(values, list):
        return [[str(values)]]
    result = []
    for row in values:
        if not isinstance(row, list):
            result.append([str(row)])
        else:
            result.append([str(v) if v is not None else "" for v in row])
    return result


def _extract_drive_file_ids(text: str) -> list[str]:
    """Extract all Drive file IDs from a string that may contain multiple URLs."""
    if not text:
        return []
    ids = []
    # Match URLs with ?id= or &id=
    ids.extend(re.findall(r"[?&]id=([a-zA-Z0-9_-]+)", text))
    # Match /file/d/FILE_ID
    ids.extend(re.findall(r"/file/d/([a-zA-Z0-9_-]+)", text))
    # If nothing matched but the input looks like a raw ID, use it
    if not ids:
        for token in re.split(r"[,\s]+", text.strip()):
            token = token.strip()
            if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", token):
                ids.append(token)
    # Dedupe while preserving order
    seen = set()
    result = []
    for fid in ids:
        if fid not in seen:
            seen.add(fid)
            result.append(fid)
    return result


def _analyze_single_proof(file_id: str) -> dict:
    """Download and analyze a single PayNow image. Returns parsed JSON dict (with error fields on failure)."""
    try:
        img_b64, _ = get_image_base64(file_id)
        img_bytes = base64.b64decode(img_b64)
        img = PIL.Image.open(io.BytesIO(img_bytes))
        img.load()
    except Exception as e:
        return {"error": f"Could not download/read image: {e}"}

    paynow_prompt = (
        "You are verifying a PayNow payment screenshot for a youth camp registration fee. "
        "Analyze this image and respond ONLY with a JSON object (no markdown, no extra text) "
        "in this exact format:\n\n"
        "{\n"
        '  "is_paynow_receipt": true|false,\n'
        '  "amount": <number or null>,\n'
        '  "currency": "SGD" or other code or null,\n'
        '  "recipient_name": "<name or null>",\n'
        '  "date": "<date string or null>",\n'
        '  "reference": "<transaction ref or null>",\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "issue": "<description of what is wrong, or null if receipt is valid>"\n'
        "}\n\n"
        "Rules:\n"
        "- is_paynow_receipt=false if the image is NOT a PayNow/bank transfer receipt.\n"
        "- amount=null if you cannot clearly read a numeric amount.\n"
        "- confidence=low if blurry or partial.\n"
        "- Be strict. If you're not sure, mark is_paynow_receipt=false."
    )

    try:
        vision_response = model.generate_content([paynow_prompt, img])
        raw = (vision_response.text or "").strip()
    except ResourceExhausted:
        return {"error": "Rate limited"}
    except Exception as e:
        return {"error": f"Vision error: {e}"}

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"error": f"Could not parse response: {raw[:200]}"}


def _send_with_retry(chat, message, max_retries=3):
    """Send a message to Gemini chat, retrying on rate limits."""
    for attempt in range(max_retries):
        try:
            return chat.send_message(message)
        except ResourceExhausted as e:
            match = re.search(r"retry in ([\d.]+)", str(e))
            wait = float(match.group(1)) + 1 if match else (attempt + 1) * 15
            logger.warning(f"Rate limited, waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                raise
            time.sleep(wait)


# ── Tool handlers ─────────────────────────────────────────────────────
def handle_tool(name: str, args: dict) -> str:
    logger.info(f"Tool call: {name}({args})")

    try:
        if name == "read_sheet":
            data = read_sheet()
            if not data:
                return "The sheet is empty."
            return (
                f"Sheet data ({len(data)} rows including header):\n"
                + "\n".join(["\t".join(str(c) for c in row) for row in data])
            )

        elif name == "write_sheet":
            range_ = args.get("range", "")
            values = _stringify_values(args.get("values", []))
            if not range_:
                return "Error: range is required."
            if not values:
                return "Error: values cannot be empty."
            write_sheet(range_, values)
            return f"Sheet updated at range {range_}."

        elif name == "append_row":
            values = args.get("values", [])
            if not isinstance(values, list) or not values:
                return "Error: values must be a non-empty list."
            values_2d = _stringify_values([values])
            append_sheet(values_2d)
            return f"Row appended: {values_2d[0]}"

        elif name == "add_column":
            header = args.get("header", "").strip()
            if not header:
                return "Error: header text is required."
            col_letter = add_column(header)
            return f"New column '{header}' added as column {col_letter}."

        elif name == "list_images":
            files = list_drive_images()
            if not files:
                return "No images found in the Drive folder."
            return "Images in Drive folder:\n" + "\n".join(
                [f"• {f['name']}" for f in files]
            )

        elif name == "analyze_payment_proof":
            raw_input = args.get("drive_urls_or_ids", "").strip()
            camper_name = args.get("camper_name", "").strip()
            if not raw_input:
                return "Error: drive_urls_or_ids is required."

            file_ids = _extract_drive_file_ids(raw_input)
            if not file_ids:
                return f"Could not extract any Drive file ID from '{raw_input[:200]}'. Please provide a valid Drive URL, file ID, or comma-separated list."

            who = f" for {camper_name}" if camper_name else ""

            # Analyze each image
            results = []
            for fid in file_ids:
                results.append(_analyze_single_proof(fid))

            # Build report
            total = 0.0
            valid_count = 0
            invalid_count = 0
            unreadable_count = 0
            error_count = 0
            currency = ""
            lines = [f"📋 Payment proof analysis{who} ({len(file_ids)} image{'s' if len(file_ids) != 1 else ''}):\n"]

            for i, data in enumerate(results, 1):
                header_line = f"Image {i}:"

                if "error" in data:
                    lines.append(f"{header_line} ⚠️ {data['error']}")
                    error_count += 1
                    continue

                is_receipt = data.get("is_paynow_receipt")
                amount = data.get("amount")
                cur = data.get("currency") or ""
                recipient = data.get("recipient_name")
                date = data.get("date")
                ref = data.get("reference")
                conf = data.get("confidence", "unknown")
                issue = data.get("issue")

                if not is_receipt:
                    lines.append(f"{header_line} ❌ NOT a valid PayNow receipt ({issue or 'wrong image type'})")
                    invalid_count += 1
                    continue

                if amount is None:
                    lines.append(f"{header_line} ⚠️ Receipt detected but amount unreadable ({issue or 'unclear'}, confidence: {conf})")
                    unreadable_count += 1
                    continue

                # Valid receipt with amount
                valid_count += 1
                try:
                    total += float(amount)
                except (TypeError, ValueError):
                    pass
                if cur and not currency:
                    currency = cur

                detail = f"{header_line} ✅ {cur} {amount}".strip()
                extras = []
                if recipient:
                    extras.append(f"to {recipient}")
                if date:
                    extras.append(date)
                if ref:
                    extras.append(f"ref {ref}")
                if extras:
                    detail += f" ({', '.join(extras)})"
                if conf != "high":
                    detail += f" [confidence: {conf}]"
                lines.append(detail)

            # Summary
            lines.append("")
            lines.append("─" * 20)
            if valid_count > 0:
                lines.append(f"💰 Total valid payments: {currency} {total:g}".strip())
            lines.append(f"✅ Valid: {valid_count}  ❌ Invalid: {invalid_count}  ⚠️ Unreadable: {unreadable_count}  ⛔ Errors: {error_count}")

            if invalid_count or unreadable_count:
                lines.append("")
                lines.append("⚠️ Some images had issues — please review manually or ask camper to re-upload.")

            return "\n".join(lines)

        return f"Unknown tool: {name}"

    except HttpError as e:
        # Google Sheets / Drive API errors
        status = e.resp.status if hasattr(e, "resp") else "?"
        if status == 403:
            return "Permission denied. Make sure the service account has access to the sheet/folder."
        elif status == 404:
            return "Resource not found. Check the spreadsheet ID, sheet tab name, or range."
        elif status == 400:
            return f"Invalid request to Google: {e._get_reason() if hasattr(e, '_get_reason') else str(e)}"
        else:
            return f"Google API error ({status}): {e._get_reason() if hasattr(e, '_get_reason') else str(e)}"
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}", exc_info=True)
        return f"Tool '{name}' failed: {e}"


# ── Main entry point ──────────────────────────────────────────────────
def ask_gemini(user_message: str) -> str:
    if not user_message or not user_message.strip():
        return "Please send a question or command."

    chat = model.start_chat()

    try:
        response = _send_with_retry(chat, user_message)
    except ResourceExhausted:
        return "Rate limit hit. Please wait a minute and try again."
    except GoogleAPIError as e:
        logger.error(f"Gemini API error: {e}", exc_info=True)
        return f"AI error: {e}"

    max_iterations = 10  # prevent infinite tool-call loops
    for _ in range(max_iterations):
        try:
            part = response.candidates[0].content.parts[0]
        except (IndexError, AttributeError):
            return "I couldn't generate a response. Please try rephrasing."

        if part.function_call and part.function_call.name:
            fc = part.function_call
            args = _proto_to_native(fc.args) if fc.args else {}
            if not isinstance(args, dict):
                args = {}
            result = handle_tool(fc.name, args)

            try:
                response = _send_with_retry(chat,
                    genai.protos.Content(parts=[
                        genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name=fc.name,
                                response={"result": result}
                            )
                        )
                    ])
                )
            except ResourceExhausted:
                return f"Rate limit hit mid-request. Partial result:\n{result}"
        else:
            text = getattr(response, "text", None) or ""
            return text.strip() or "Done."

    return "I got stuck in a loop. Please try rephrasing your request."