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
You have access to the camp's Google Sheet (registration data) and a Google Drive folder (PayNow payment receipts uploaded via Google Form).

The sheet tab is called '{SHEET_NAME}' — always use this as the sheet name in A1 notation (e.g. {SHEET_NAME}!B2).

Guidelines:
- Keep replies concise and friendly — this is a Telegram chat
- The first row of the sheet is always the header
- When editing cells, ALWAYS call read_sheet first if you need to know column positions or row numbers
- When adding a new column (e.g. "Fees paid"), use add_column — don't manually write to an empty column
- The analyze_image tool is designed for PayNow receipts — it extracts the amount paid and validates the image.
  If it returns "not a valid PayNow receipt" or "no amount", relay that to the user verbatim — do NOT guess the amount.
- If unsure which image the user means, call list_images first
- Never expose raw JSON or cell coordinates — summarize in plain language
- If a user asks to modify data, confirm what you did (which row/column) after the action
- Do NOT use markdown formatting like *bold* or _italic_ — plain text only, Telegram will display it as-is"""

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

analyze_image_fn = FunctionDeclaration(
    name="analyze_image",
    description=(
        "Download and analyze a PayNow payment screenshot from Google Drive. "
        "Returns the transferred amount, recipient, date, and reference number if found. "
        "Flags the image if it's not a valid payment receipt."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": "The exact or partial filename of the image in Google Drive"
            }
        },
        "required": ["file_name"]
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
    analyze_image_fn,
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

        elif name == "analyze_image":
            query = args.get("file_name", "").lower().strip()
            if not query:
                return "Error: file_name is required."
            files = list_drive_images()
            if not files:
                return "There are no images in the Drive folder."
            match = next((f for f in files if query in f["name"].lower()), None)
            if not match:
                available = ", ".join(f["name"] for f in files[:10])
                more = f" (and {len(files) - 10} more)" if len(files) > 10 else ""
                return f"Image '{args['file_name']}' not found. Available: {available}{more}"

            img_b64, mime_type = get_image_base64(match["id"])
            try:
                img_bytes = base64.b64decode(img_b64)
                img = PIL.Image.open(io.BytesIO(img_bytes))
                img.load()
            except Exception as e:
                return f"⚠️ Could not read image '{match['name']}': {e}. The file may be corrupted or not a real image."

            # Strict PayNow receipt analysis with JSON output
            paynow_prompt = (
                "You are verifying a PayNow payment screenshot for a youth camp registration fee. "
                "Analyze this image and respond ONLY with a JSON object (no markdown, no explanation outside the JSON) "
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
                "- Set is_paynow_receipt=false if the image is NOT a PayNow/bank transfer receipt "
                "(e.g. it's a photo, form, document, screenshot of something else, or blank).\n"
                "- Set amount=null if you cannot clearly read a numeric amount.\n"
                "- Set confidence=low if the image is blurry, partial, or hard to read.\n"
                "- Explain in 'issue' if something is wrong (wrong image type, no amount visible, "
                "looks fake, amount unclear, etc.) — otherwise set issue=null.\n"
                "- Be strict. If you're not sure it's a real PayNow receipt, say is_paynow_receipt=false."
            )

            try:
                vision_response = model.generate_content([paynow_prompt, img])
                raw = (vision_response.text or "").strip()
            except ResourceExhausted:
                return f"Rate limit hit while analyzing {match['name']}. Please try again in a minute."

            # Strip markdown code fences if Gemini wraps the JSON
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                return (
                    f"⚠️ Could not parse payment info from '{match['name']}'.\n"
                    f"Raw AI response:\n{raw[:500]}"
                )

            # Build a human-friendly summary from the structured data
            is_receipt = data.get("is_paynow_receipt")
            amount = data.get("amount")
            currency = data.get("currency") or ""
            recipient = data.get("recipient_name")
            date = data.get("date")
            ref = data.get("reference")
            confidence = data.get("confidence", "unknown")
            issue = data.get("issue")

            if not is_receipt:
                return (
                    f"❌ '{match['name']}' does NOT appear to be a valid PayNow receipt.\n"
                    f"Issue: {issue or 'Not identified as a payment screenshot'}\n"
                    f"Please ask the camper to re-upload their actual payment screenshot."
                )

            if amount is None:
                return (
                    f"⚠️ '{match['name']}' looks like a payment screenshot but no amount could be read.\n"
                    f"Issue: {issue or 'Amount not visible or illegible'}\n"
                    f"Confidence: {confidence}\n"
                    f"Please review the image manually or request a clearer screenshot."
                )

            # Valid receipt with amount
            lines = [f"✅ PayNow receipt detected for '{match['name']}':"]
            lines.append(f"Amount: {currency} {amount}".strip())
            if recipient:
                lines.append(f"Recipient: {recipient}")
            if date:
                lines.append(f"Date: {date}")
            if ref:
                lines.append(f"Reference: {ref}")
            lines.append(f"Confidence: {confidence}")
            if issue:
                lines.append(f"⚠️ Note: {issue}")
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