import os
import anthropic
from google_services import read_sheet, write_sheet, list_drive_images, get_image_base64

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a helpful assistant managing a youth camp.
You have access to the camp's Google Sheet (registration data) and a Google Drive folder (uploaded images from the Google Form).

Guidelines:
- Keep replies concise and friendly — this is a Telegram chat
- Use bullet points or numbered lists where helpful
- When reading the sheet, the first row is always the header
- When editing, be precise about the cell range (e.g. Sheet1!C5)
- When analyzing images, describe what you see and flag anything relevant to camp admin (e.g. parental consent forms, ID photos, payment receipts)
- If you're unsure which image the user means, list available images first
- Never expose raw JSON — always summarize data in plain language"""

TOOLS = [
    {
        "name": "read_sheet",
        "description": "Read all rows from the camp registration Google Sheet. Returns all data including headers.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "write_sheet",
        "description": "Write or update specific cells in the Google Sheet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "description": "Cell range in A1 notation, e.g. 'Sheet1!B2' or 'Sheet1!C2:C5'"
                },
                "values": {
                    "type": "array",
                    "description": "2D array of values to write. E.g. [['Paid'], ['Paid']] for a column update.",
                    "items": {"type": "array"}
                }
            },
            "required": ["range", "values"]
        }
    },
    {
        "name": "list_images",
        "description": "List all image files uploaded to the Google Drive folder linked to the form.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "analyze_image",
        "description": "Download and analyze an image from Google Drive using vision. Use this to read forms, check documents, or describe photos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "The exact or partial filename of the image in Google Drive"
                }
            },
            "required": ["file_name"]
        }
    }
]


def handle_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "read_sheet":
        data = read_sheet()
        if not data:
            return "The sheet is empty."
        return f"Sheet data ({len(data)} rows including header):\n" + "\n".join(
            ["\t".join(row) for row in data]
        )

    elif tool_name == "write_sheet":
        write_sheet(tool_input["range"], tool_input["values"])
        return f"✅ Sheet updated at range {tool_input['range']}."

    elif tool_name == "list_images":
        files = list_drive_images()
        if not files:
            return "No images found in the Drive folder."
        return "Images in Drive folder:\n" + "\n".join(
            [f"• {f['name']} (id: {f['id']})" for f in files]
        )

    elif tool_name == "analyze_image":
        files = list_drive_images()
        query = tool_input["file_name"].lower()
        match = next((f for f in files if query in f["name"].lower()), None)
        if not match:
            available = ", ".join(f["name"] for f in files) or "none"
            return f"Image '{tool_input['file_name']}' not found. Available: {available}"

        img_b64, mime_type = get_image_base64(match["id"])

        # Sub-call Claude vision to analyze the image
        vision_response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": img_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are analyzing an image uploaded via a youth camp registration form. "
                            "Describe what you see in detail. Note any names, dates, signatures, "
                            "payment info, or anything relevant to camp administration."
                        )
                    }
                ]
            }]
        )
        return f"📷 Analysis of *{match['name']}*:\n{vision_response.content[0].text}"

    return f"Unknown tool: {tool_name}"


def ask_claude(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            # Extract final text response
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else "Done."

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        else:
            # Unexpected stop reason
            return "Unexpected response from AI. Please try again."
