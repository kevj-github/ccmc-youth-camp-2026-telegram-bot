import os
import json
import base64
import io
import logging

import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
import PIL.Image

from google_services import read_sheet, write_sheet, list_drive_images, get_image_base64

logger = logging.getLogger(__name__)

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

SYSTEM_PROMPT = """You are a helpful assistant managing a youth camp.
You have access to the camp's Google Sheet (registration data) and a Google Drive folder (images uploaded via Google Form).

Guidelines:
- Keep replies concise and friendly — this is a Telegram chat
- Use bullet points or numbered lists where helpful
- The first row of the sheet is always the header
- When editing, be precise about the cell range (e.g. Sheet1!C5)
- When analyzing images, describe what you see and flag anything relevant to camp admin
- If unsure which image the user means, list available images first
- Never expose raw JSON — always summarize in plain language"""

# --- Tool definitions ---
read_sheet_fn = FunctionDeclaration(
    name="read_sheet",
    description="Read all rows from the camp registration Google Sheet. Returns all data including headers.",
    parameters={"type": "object", "properties": {}}
)

write_sheet_fn = FunctionDeclaration(
    name="write_sheet",
    description="Write or update specific cells in the Google Sheet. values is a 2D array e.g. [['Paid'], ['Paid']] for a single column.",
    parameters={
        "type": "object",
        "properties": {
            "range": {
                "type": "string",
                "description": "Cell range in A1 notation, e.g. Sheet1!B2 or Sheet1!C2:C5"
            },
            "values": {
                "type": "array",
                "description": "2D array of string values to write.",
                "items": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        "required": ["range", "values"]
    }
)

list_images_fn = FunctionDeclaration(
    name="list_images",
    description="List all image files uploaded to the Google Drive folder linked to the form.",
    parameters={"type": "object", "properties": {}}
)

analyze_image_fn = FunctionDeclaration(
    name="analyze_image",
    description="Download and analyze an image from Google Drive. Use this to read forms, check documents, or describe photos.",
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

camp_tools = Tool(function_declarations=[
    read_sheet_fn,
    write_sheet_fn,
    list_images_fn,
    analyze_image_fn
])

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT,
    tools=[camp_tools]
)


def handle_tool(name: str, args: dict) -> str:
    logger.info(f"Tool call: {name}({args})")

    if name == "read_sheet":
        data = read_sheet()
        if not data:
            return "The sheet is empty."
        return f"Sheet data ({len(data)} rows including header):\n" + "\n".join(
            ["\t".join(row) for row in data]
        )

    elif name == "write_sheet":
        write_sheet(args["range"], args["values"])
        return f"Sheet updated at range {args['range']}."

    elif name == "list_images":
        files = list_drive_images()
        if not files:
            return "No images found in the Drive folder."
        return "Images in Drive folder:\n" + "\n".join(
            [f"• {f['name']}" for f in files]
        )

    elif name == "analyze_image":
        files = list_drive_images()
        query = args["file_name"].lower()
        match = next((f for f in files if query in f["name"].lower()), None)

        if not match:
            available = ", ".join(f["name"] for f in files) or "none"
            return f"Image '{args['file_name']}' not found. Available files: {available}"

        img_b64, mime_type = get_image_base64(match["id"])
        img_bytes = base64.b64decode(img_b64)
        img = PIL.Image.open(io.BytesIO(img_bytes))

        vision_response = model.generate_content([
            "You are analyzing an image uploaded via a youth camp registration form. "
            "Describe what you see in detail. Note any names, dates, signatures, "
            "payment info, or anything relevant to camp administration.",
            img
        ])
        return f"Analysis of {match['name']}:\n{vision_response.text}"

    return f"Unknown tool: {name}"


def ask_gemini(user_message: str) -> str:
    chat = model.start_chat()
    response = chat.send_message(user_message)

    while True:
        part = response.candidates[0].content.parts[0]

        if part.function_call.name:
            fc = part.function_call
            result = handle_tool(fc.name, dict(fc.args))

            response = chat.send_message(
                genai.protos.Content(parts=[
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": result}
                        )
                    )
                ])
            )
        else:
            return response.text