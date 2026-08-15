#!/usr/bin/env python3
"""
Telegram bot for Templatea - mimics frontend_cli.py functionality.

Setup:
1. Uninstall wrong package and install correct one:
   pip uninstall telegram
   pip install python-telegram-bot==20.7 httpx
2. Set environment variables:
   - TELEGRAM_BOT_TOKEN: Your bot token from @BotFather
   - API_BASE_URL: Backend API URL (default: http://localhost:8000)
   - API_KEY: Your API key if backend requires authentication
3. Run: python telegram_bot.py

Features:
- User sends Instagram reel URL
- Bot shows available templates
- Bot processes the reel and shows OCR/AI copy options
- User selects final copy
- Bot sends the rendered video
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional
from datetime import datetime
from io import BytesIO
from dotenv import load_dotenv

load_dotenv() 

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
PUBLIC_FILE_BASE_URL = os.getenv("PUBLIC_FILE_BASE_URL", API_BASE_URL)
API_KEY = os.getenv("API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")

# Conversation states
STATE_WAITING_URL = 1
STATE_SELECTING_TEMPLATE = 2
STATE_WAITING_PROCESSING = 3
STATE_SELECTING_COPY = 4

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Emoji remover (same as orchestrator)
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE
)

def clean_text(text: str) -> str:
    """Remove emojis and clean text."""
    if not text:
        return ""
    t = EMOJI_RE.sub("", text)
    t = " ".join(t.splitlines())
    t = " ".join(t.split())
    return t.strip()


def normalize_for_render(text: str) -> str:
    """Mirror orchestrator text normalization so comparisons match render metadata."""
    if not text:
        return ""
    replaced = text.replace("\u2014", ", ").replace("\u2013", ", ")
    return clean_text(replaced)


def build_absolute_url(url: str, base: str) -> str:
    """Normalize relative API paths into absolute URLs based on the provided base."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    base = base.rstrip("/")
    if not url.startswith("/"):
        url = f"/{url}"
    return f"{base}{url}"


class TemplateaAPI:
    """Client for Templatea backend API."""
    
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=600.0)
    
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers
    
    async def list_templates(self) -> List[Dict]:
        """Get available templates."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/api/v1/templates",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to list templates: {e}")
            return []
    
    async def create_reel(self, url: str, template_id: str, auto: bool = False) -> Optional[Dict]:
        """Submit a new reel for processing."""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/v1/reels",
                json={"url": url, "template_id": template_id, "auto": auto},
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to create reel: {e}")
            return None
    
    async def get_workspace(self, workspace_id: str) -> Optional[Dict]:
        """Get workspace details."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/api/v1/workspaces/{workspace_id}",
                headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get workspace: {e}")
            return None
    
    async def submit_choice(self, workspace_id: str, choice_type: str, text: str) -> bool:
        """Submit user's copy choice."""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/v1/workspaces/{workspace_id}/choice",
                json={"type": choice_type, "text": text},
                headers=self._headers()
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to submit choice: {e}")
            return False
    
    async def get_file_url(self, workspace_id: str, file_key: str) -> str:
        """Get URL for a workspace file."""
        return f"{self.base_url}/api/v1/workspaces/{workspace_id}/files/{file_key}"
    
    async def download_file(self, url: str) -> Optional[bytes]:
        """Download a file from the API with retries and diagnostics."""
        attempts = 100
        delay_seconds = 2
        last_error: Optional[str] = None

        for attempt in range(1, attempts + 1):
            full_url = url if url.startswith("http") else f"{self.base_url}{url}"
            try:
                resp = await self.client.get(full_url, headers=self._headers())
                if resp.status_code == 200:
                    return resp.content

                snippet = resp.text[:200] if resp.text else ""
                last_error = (
                    f"HTTP {resp.status_code} while downloading {full_url} "
                    f"(attempt {attempt}/{attempts}); payload snippet: {snippet!r}"
                )
                logger.warning(last_error)
            except Exception as exc:
                last_error = f"Exception downloading {full_url} (attempt {attempt}/{attempts}): {exc}"
                logger.warning(last_error)

            if attempt < attempts:
                logger.info(
                    f"Retrying download for {full_url} after {delay_seconds}s "
                    f"(attempt {attempt + 1}/{attempts})"
                )
                await asyncio.sleep(delay_seconds)

        if last_error:
            logger.error(last_error)
        return None
    
    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


async def send_with_retry(description: str, coro_factory):
    """Retry Telegram API calls that may time out."""
    attempts = 3
    delay_seconds = 2
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except (TimedOut, NetworkError) as exc:
            logger.warning(
                "%s failed (attempt %s/%s): %s",
                description,
                attempt,
                attempts,
                exc,
            )
            if attempt == attempts:
                raise
            await asyncio.sleep(delay_seconds)


async def reply_text_with_retry(message, *args, **kwargs):
    return await send_with_retry(
        "reply_text", lambda: message.reply_text(*args, **kwargs)
    )


async def reply_video_with_retry(message, *args, **kwargs):
    return await send_with_retry(
        "reply_video", lambda: message.reply_video(*args, **kwargs)
    )


async def reply_document_with_retry(message, *args, **kwargs):
    return await send_with_retry(
        "reply_document", lambda: message.reply_document(*args, **kwargs)
    )


async def edit_text_with_retry(message, *args, **kwargs):
    return await send_with_retry(
        "edit_text", lambda: message.edit_text(*args, **kwargs)
    )


async def edit_message_text_with_retry(query, *args, **kwargs):
    return await send_with_retry(
        "edit_message_text", lambda: query.edit_message_text(*args, **kwargs)
    )


# Global API client
api = TemplateaAPI(API_BASE_URL, API_KEY)


async def send_caption_text_if_available(message, workspace_id: str, files_snapshot: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """Fetch caption.txt for the workspace and send it as text if available."""
    try:
        files = files_snapshot
        if files is None:
            workspace = await api.get_workspace(workspace_id)
            if not workspace:
                logger.warning("Workspace %s not found while trying to send caption", workspace_id)
                await reply_text_with_retry(
                    message,
                    "Error generating caption file, sorry for the inconvenience."
                )
                return False
            files = workspace.get("files", {}) or {}

        caption_entry = files.get("caption") or {}
        caption_url = caption_entry.get("url")
        if not caption_url:
            await reply_text_with_retry(
                message,
                "Error generating caption file, sorry for the inconvenience."
            )
            return False

        caption_data = await api.download_file(caption_url)
        if not caption_data:
            await reply_text_with_retry(
                message,
                "Error generating caption file, sorry for the inconvenience."
            )
            return False

        caption_text = caption_data.decode("utf-8", errors="ignore").strip()
        if not caption_text:
            await reply_text_with_retry(
                message,
                "Error generating caption file, sorry for the inconvenience."
            )
            return False

        await reply_text_with_retry(
            message,
            f"{caption_text}"
        )
        return True
    except Exception as exc:
        logger.error("Failed to send caption file for %s: %s", workspace_id, exc)
        await reply_text_with_retry(
            message,
            "Error generating caption file, sorry for the inconvenience."
        )
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation."""
    user = update.effective_user
    await reply_text_with_retry(
        update.message,
        f"👋 Hi {user.first_name}! Welcome to Templatea Bot.\n\n"
        "I'll help you create awesome marketing videos from Instagram reels.\n\n"
        "📱 Send me an Instagram reel URL to get started!\n\n"
        "Use /cancel to stop at any time."
    )
    return STATE_WAITING_URL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await reply_text_with_retry(
        update.message,
        "Operation cancelled. Send me a new Instagram URL whenever you're ready!",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive Instagram URL and show template selection."""
    url = update.message.text.strip()
    
    # Basic URL validation
    if not ("instagram.com" in url or "instagr.am" in url):
        await reply_text_with_retry(
            update.message,
            "❌ That doesn't look like an Instagram URL. Please send a valid Instagram reel link."
        )
        return STATE_WAITING_URL
    
    context.user_data["url"] = url
    
    # Show loading message
    loading_msg = await reply_text_with_retry(update.message, "🔍 Loading templates...")
    
    # Fetch templates
    templates = await api.list_templates()
    
    if not templates:
        await edit_text_with_retry(
            loading_msg,
            "❌ No templates available. Please contact the administrator."
        )
        return ConversationHandler.END
    
    # Store templates
    context.user_data["templates"] = templates
    
    # Create inline keyboard with templates
    keyboard = []
    for template in templates:
        template_id = template.get("id")
        template_name = template.get("name", template_id)
        keyboard.append([
            InlineKeyboardButton(
                f"🎨 {template_name}",
                callback_data=f"template:{template_id}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await edit_text_with_retry(
        loading_msg,
        "✅ URL received!\n\n"
        "📋 Please select a template:",
        reply_markup=reply_markup
    )
    
    return STATE_SELECTING_TEMPLATE


async def template_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle template selection and start processing."""
    query = update.callback_query
    await query.answer()
    
    template_id = query.data.split(":", 1)[1]
    context.user_data["template_id"] = template_id
    
    # Get template name for display
    templates = context.user_data.get("templates", [])
    template_name = next(
        (t.get("name", template_id) for t in templates if t.get("id") == template_id),
        template_id
    )
    
    await edit_message_text_with_retry(
        query,
        f"✅ Template selected: **{template_name}**\n\n"
        "⏳ Starting processing... This may take a few minutes.\n\n"
        "_Note: If you're reprocessing the same video, any cached renders will be cleared._"
    )
    
    # Submit to API
    url = context.user_data["url"]
    result = await api.create_reel(url, template_id, auto=False)
    
    if not result or not result.get("ok"):
        await reply_text_with_retry(
            query.message,
            "❌ Failed to start processing. Please try again or contact support."
        )
        return ConversationHandler.END
    
    workspace = result.get("workspace", {})
    workspace_id = workspace.get("id")
    
    if not workspace_id:
        await reply_text_with_retry(
            query.message,
            "❌ No workspace created. Please try again."
        )
        return ConversationHandler.END
    
    context.user_data["workspace_id"] = workspace_id
    
    # Start polling for status
    await reply_text_with_retry(
        query.message,
        "⚙️ Processing your reel...\n"
        "📥 Downloading video\n"
        "🎬 Detecting content\n"
        "📝 Extracting text with AI\n\n"
        "This will take 1-3 minutes..."
    )
    
    # Poll for OCR completion
    success = await poll_for_ocr(query.message, workspace_id, context)
    
    if not success:
        return ConversationHandler.END
    
    return STATE_SELECTING_COPY


async def poll_for_ocr(message, workspace_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Poll workspace until OCR is complete."""
    max_attempts = 180  # 3 minutes with 1-second intervals
    attempt = 0
    last_status = None
    
    while attempt < max_attempts:
        workspace = await api.get_workspace(workspace_id)
        
        if not workspace:
            await reply_text_with_retry(message, "❌ Failed to check processing status.")
            return False
        
        status = workspace.get("status", {})
        ocr_status = status.get("02_ocr")
        
        # Update user if status changed
        if ocr_status != last_status:
            last_status = ocr_status
            if ocr_status == "success" or ocr_status == "fallback_caption":
                break
            elif ocr_status == "failed":
                await reply_text_with_retry(message, "❌ OCR processing failed. Please try again.")
                return False
        
        attempt += 1
        await asyncio.sleep(1)
    
    if attempt >= max_attempts:
        await reply_text_with_retry(
            message,
            "⏱️ Processing is taking longer than expected. "
            "Please check back later or contact support."
        )
        return False
    
    # Get copy options
    workspace = await api.get_workspace(workspace_id)
    files = workspace.get("files", {})
    
    # Load OCR and AI copies
    ocr_text = ""
    ai_copies = []
    
    # Try to get OCR text
    ocr_url = files.get("ocr", {}).get("url")
    if ocr_url:
        ocr_data = await api.download_file(ocr_url)
        if ocr_data:
            ocr_text = ocr_data.decode("utf-8", errors="ignore").strip()
    
    # Try to get AI copies
    ai_url = files.get("ai_copies", {}).get("url")
    if ai_url:
        ai_data = await api.download_file(ai_url)
        if ai_data:
            try:
                import json
                ai_copies = json.loads(ai_data.decode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to parse AI copies: {e}")
    
    # Store options
    context.user_data["ocr_text"] = clean_text(ocr_text)
    context.user_data["ai_copies"] = ai_copies
    
    # Build selection keyboard
    keyboard = []
    
    # Manual entry option
    keyboard.append([InlineKeyboardButton("✍️ Enter manual copy", callback_data="copy:manual")])
    
    # OCR option
    if ocr_text:
        cleaned = clean_text(ocr_text)
        preview = cleaned[:50] + "..." if len(cleaned) > 50 else cleaned
        keyboard.append([
            InlineKeyboardButton(
                f"📝 OCR: {preview}",
                callback_data="copy:ocr"
            )
        ])
    
    # AI options
    for idx, ai_copy in enumerate(ai_copies[:5]):  # Limit to 5 options
        if isinstance(ai_copy, dict):
            text = ai_copy.get("text", "")
        else:
            text = str(ai_copy)
        
        cleaned = clean_text(text)
        preview = cleaned[:50] + "..." if len(cleaned) > 50 else cleaned
        keyboard.append([
            InlineKeyboardButton(
                f"🤖 AI {idx+1}: {preview}",
                callback_data=f"copy:ai:{idx}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await reply_text_with_retry(
        message,
        "✅ Processing complete!\n\n"
        "📋 Please select the copy you want to use for your video:",
        reply_markup=reply_markup
    )
    
    return True


async def copy_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle copy selection."""
    query = update.callback_query
    await query.answer()
    
    choice_data = query.data.split(":", 2)
    choice_type = choice_data[1]
    
    if choice_type == "manual":
        await edit_message_text_with_retry(
            query,
            "✍️ Please type or paste your custom copy text:"
        )
        context.user_data["awaiting_manual"] = True
        return STATE_SELECTING_COPY
    
    # Get selected text
    selected_text = ""
    
    if choice_type == "ocr":
        selected_text = context.user_data.get("ocr_text", "")
    elif choice_type == "ai":
        ai_index = int(choice_data[2])
        ai_copies = context.user_data.get("ai_copies", [])
        if ai_index < len(ai_copies):
            ai_copy = ai_copies[ai_index]
            if isinstance(ai_copy, dict):
                selected_text = ai_copy.get("text", "")
            else:
                selected_text = str(ai_copy)
            selected_text = clean_text(selected_text)
    
    if not selected_text:
        await edit_message_text_with_retry(
            query,
            "❌ Failed to get selected copy. Please try again."
        )
        return ConversationHandler.END
    
    return await finalize_choice(query.message, selected_text, choice_type, context)


async def receive_manual_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive manual copy text from user."""
    if not context.user_data.get("awaiting_manual"):
        return STATE_SELECTING_COPY
    
    manual_text = update.message.text.strip()
    cleaned_text = clean_text(manual_text)
    
    if not cleaned_text:
        await reply_text_with_retry(
            update.message,
            "❌ Empty text. Please send a valid copy text."
        )
        return STATE_SELECTING_COPY
    
    context.user_data["awaiting_manual"] = False
    return await finalize_choice(update.message, cleaned_text, "manual", context)


async def finalize_choice(message, selected_text: str, choice_type: str, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Submit choice and wait for render."""
    workspace_id = context.user_data["workspace_id"]
    
    # Show preview
    preview = selected_text[:100] + "..." if len(selected_text) > 100 else selected_text
    await reply_text_with_retry(
        message,
        f"✅ Copy selected:\n\n\"{preview}\"\n\n"
        "🎬 Rendering your video... This will take 1-2 minutes."
    )
    
    # Submit choice
    success = await api.submit_choice(workspace_id, choice_type, selected_text)
    
    if not success:
        await reply_text_with_retry(
            message,
            "❌ Failed to submit choice. Please try again."
        )
        return ConversationHandler.END
    
    # Wait a moment for the backend to process the choice
    await asyncio.sleep(2)
    submitted_at = datetime.utcnow()
    selected_clean = normalize_for_render(selected_text)
    
    # Poll for render completion
    max_attempts = 300  # 5 minutes
    max_missing_status_attempts = 180  # ~2 minutes without any render status
    attempt = 0
    last_status = None
    missing_status_count = 0
    
    while attempt < max_attempts:
        workspace = await api.get_workspace(workspace_id)
        
        if not workspace:
            await reply_text_with_retry(message, "❌ Failed to check render status.")
            return ConversationHandler.END
        
        status = workspace.get("status", {})
        render_status = status.get("04_render")
        
        if not render_status:
            missing_status_count += 1
            if missing_status_count >= max_missing_status_attempts:
                await reply_text_with_retry(
                    message,
                    "⏱️ Rendering is taking longer than expected and no progress is reported yet.\n"
                    "Please check back later or contact support."
                )
                return ConversationHandler.END
        else:
            missing_status_count = 0
        
        # Update user on status changes
        if render_status != last_status and render_status:
            last_status = render_status
            logger.info(f"Render status changed to: {render_status}")
        
        if render_status == "success":
            meta = workspace.get("meta", {})
            render_info = meta.get("render", {})
            rendered_text = normalize_for_render(render_info.get("text", ""))
            if rendered_text != selected_clean:
                logger.info(
                    "Render reported success but text mismatched (expected=%r, got=%r); awaiting updated render.",
                    selected_clean,
                    rendered_text,
                )
            else:
                render_timestamp = render_info.get("ts", "")
                logger.info(
                    "Render complete - Selected: '%s' | Rendered: '%s'",
                    selected_text[:50],
                    rendered_text[:50],
                )
                logger.info(f"Render timestamp: {render_timestamp}")
                break
        elif render_status == "failed":
            await reply_text_with_retry(message, "❌ Video rendering failed. Please try again.")
            return ConversationHandler.END
        
        attempt += 1
        await asyncio.sleep(1)
    
    if attempt >= max_attempts:
        await reply_text_with_retry(
            message,
            "⏱️ Rendering is taking longer than expected. "
            "Please check back later."
        )
        return ConversationHandler.END
    
    # Get final video
    workspace = await api.get_workspace(workspace_id)
    files = workspace.get("files", {}) if workspace else {}
    final_entry = files.get("final") or {}
    final_url = final_entry.get("url")

    if not final_url:
        # Allow a short window for the backend index to refresh after the render completes.
        for _ in range(3):
            await asyncio.sleep(1)
            workspace = await api.get_workspace(workspace_id)
            if not workspace:
                continue
            files = workspace.get("files", {})
            final_entry = files.get("final") or {}
            final_url = final_entry.get("url")
            if final_url:
                break

    candidate_urls: List[str] = []
    if final_url:
        candidate_urls.append(final_url)

    # Always include the canonical files/final endpoint as a fallback.
    fallback_url = await api.get_file_url(workspace_id, "final")
    if fallback_url and fallback_url not in candidate_urls:
        candidate_urls.append(fallback_url)

    # Normalize to absolute URLs and remove duplicates.
    normalized_urls: List[str] = []
    download_url_map: Dict[str, str] = {}
    for url in candidate_urls:
        if not url:
            continue
        absolute_api = build_absolute_url(url, API_BASE_URL)
        absolute_public = build_absolute_url(url, PUBLIC_FILE_BASE_URL)
        if absolute_api not in normalized_urls:
            normalized_urls.append(absolute_api)
        download_url_map[absolute_api] = absolute_public

    if not normalized_urls:
        await reply_text_with_retry(
            message,
            "❌ Video file not found. Please contact support."
        )
        return ConversationHandler.END
    
    await reply_text_with_retry(message, "📥 Downloading your video...")
    
    # Download video using first URL that succeeds.
    video_data = None
    download_url = None
    for url in normalized_urls:
        video_data = await api.download_file(url)
        if video_data:
            download_url = url
            break
    
    if not video_data or not download_url:
        fallback_absolute = normalized_urls[-1]
        fallback_msg = download_url_map.get(fallback_absolute, fallback_absolute)
        await reply_text_with_retry(
            message,
            f"❌ Failed to download video.\n\n"
            f"You can try downloading it directly:\n{fallback_msg}"
        )
        return ConversationHandler.END
    
    share_url = download_url_map.get(download_url, download_url)
    # Check file size (Telegram has a 50MB limit for bots)
    video_size_mb = len(video_data) / (1024 * 1024)
    
    if video_size_mb > 50:
        await reply_text_with_retry(
            message,
            f"⚠️ Video is too large ({video_size_mb:.1f}MB) to send via Telegram.\n\n"
            f"Download it here:\n{share_url}"
        )
        return ConversationHandler.END
    
    # Send video
    await reply_text_with_retry(
        message,
        f"📤 Sending your video ({video_size_mb:.1f}MB)...\n\n_This may take 1-2 minutes for large videos._"
    )
    
    try:
        video_file = BytesIO(video_data)
        video_file.name = "templatea_video.mp4"
        
        # Disable timeout protection - let it take as long as needed
        import telegram.error
        try:
            sent_message = await reply_video_with_retry(
                message,
                video=video_file,
                caption=f"✅ Your video is ready!\n\nCopy: {preview}",
                supports_streaming=True,
                write_timeout=None,
                read_timeout=None,
                connect_timeout=60.0
            )
            
            caption_sent = await send_caption_text_if_available(message, workspace_id, files)
            if caption_sent:
                await reply_text_with_retry(
                    message,
                    "dYZ% All done! Caption is above. Send me another Instagram URL to create more videos."
                )
            else:
                await reply_text_with_retry(
                    message,
                    "dYZ% All done! Send me another Instagram URL to create more videos."
                )
        except telegram.error.TimedOut:
            # Video might still be uploading - inform user
            await reply_text_with_retry(
                message,
                "⚠️ Upload is taking longer than expected, but the video is likely still being sent.\n\n"
                "If you don't receive it in 2 minutes, download here:\n"
                f"{share_url}\n\n"
                "You can send another Instagram URL to create more videos."
            )
        
    except Exception as e:
        logger.error(f"Failed to send video: {e}")
        await reply_text_with_retry(
            message,
            f"❌ Failed to send video (Error: {str(e)}).\n\n"
            f"You can download it here:\n{share_url}"
        )
        return ConversationHandler.END
    
    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    if isinstance(update, Update) and update.effective_message:
        await reply_text_with_retry(
            update.effective_message,
            "❌ An error occurred. Please try again or contact support."
        )


def main():
    """Run the bot."""
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
        ],
        states={
            STATE_WAITING_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
            ],
            STATE_SELECTING_TEMPLATE: [
                CallbackQueryHandler(template_selected, pattern="^template:")
            ],
            STATE_SELECTING_COPY: [
                CallbackQueryHandler(copy_selected, pattern="^copy:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_copy)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("Starting Templatea Telegram Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()