import subprocess
import os
import tempfile
import logging
import asyncio
import nest_asyncio
import re
import base64
import json
import urllib.request
import urllib.parse
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import TimedOut

nest_asyncio.apply()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Received /start command from user %s", update.message.from_user.id)
    keyboard = [[InlineKeyboardButton("Download a Song", callback_data='download_song')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to the Spotify Song Downloader Bot! Click the button to start downloading a song.",
        reply_markup=reply_markup
    )
    logger.info("Sent inline keyboard to user %s", update.message.from_user.id)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logger.info("Button clicked by user %s: %s", query.from_user.id, query.data)
    if query.data == 'download_song':
        await query.message.reply_text("Send a Spotify track URL (e.g. https://open.spotify.com/track/...)")
        logger.info("Prompted user %s to send a Spotify track URL", query.from_user.id)


def extract_youtube_url(text: str) -> str | None:
    """
    Look for Youtube URL in text (stdout or stderr).
    Normalize music.youtube.com -> www.youtube.com
    """
    if not text:
        return None
    # Match youtube.com/watch?v=..., music.youtube.com/watch?v=..., youtu.be/...
    pattern = r"(https?://(?:www\.|music\.)?youtube\.com/watch\?v=[\w\-\_]+(?:[&][^\s]*)?|https?://youtu\.be/[\w\-\_]+(?:\?[^ \n\r]*)?)"
    match = re.search(pattern, text)
    if not match:
        return None
    url = match.group(1)
    # Normalize music.youtube.com to www.youtube.com
    url = url.replace("music.youtube.com", "www.youtube.com")
    return url


async def run_subprocess(cmd: list[str], env=None, allow_nonzero: bool = False) -> str:
    """
    Run subprocess asynchronously.

    If allow_nonzero is True, do NOT raise on non-zero return code: return combined stdout+stderr so caller can inspect.
    If allow_nonzero is False, raise on non-zero return code (same as before).
    """
    logger.info("Running subprocess: %s", cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )
    stdout, stderr = await proc.communicate()
    out = (stdout.decode() if stdout else "") + ("\n" + stderr.decode() if stderr else "")
    if proc.returncode != 0:
        if allow_nonzero:
            logger.debug("Subprocess returned non-zero but allow_nonzero=True, returning combined output.")
            return out
        else:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"Command {cmd} failed with error: {err_msg}")
    return out


def extract_spotify_track_id(spotify_url: str) -> str | None:
    """Extract track id from spotify url"""
    m = re.search(r"track/([A-Za-z0-9]+)", spotify_url)
    return m.group(1) if m else None


def get_spotify_token_blocking(client_id: str, client_secret: str) -> str:
    """Blocking function to request Spotify client_credentials token."""
    token_url = "https://accounts.spotify.com/api/token"
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    parsed = json.loads(body.decode())
    return parsed.get("access_token")


def get_spotify_track_info_blocking(token: str, track_id: str) -> dict | None:
    """Blocking GET track info from Spotify API."""
    url = f"https://api.spotify.com/v1/tracks/{track_id}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body.decode())


async def get_spotify_search_query(spotify_url: str) -> str | None:
    """
    Use Spotify Web API (client_credentials) to fetch artist - track to use as a yt-dlp search query.
    Runs network calls in thread to avoid blocking event loop.
    """
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        logger.warning("Spotify client id/secret not configured; cannot fetch metadata fallback.")
        return None

    track_id = extract_spotify_track_id(spotify_url)
    if not track_id:
        logger.warning("Could not parse track id from url: %s", spotify_url)
        return None

    try:
        token = await asyncio.to_thread(get_spotify_token_blocking, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        if not token:
            logger.error("Failed to obtain Spotify token")
            return None
        info = await asyncio.to_thread(get_spotify_track_info_blocking, token, track_id)
        if not info:
            logger.error("Spotify track info empty")
            return None
        name = info.get("name")
        artists = info.get("artists", [])
        artist_names = ", ".join(a.get("name") for a in artists if a.get("name"))
        if name and artist_names:
            query = f"{artist_names} - {name}"
            logger.info("Built search query from Spotify metadata: %s", query)
            return query
        else:
            logger.warning("Spotify metadata missing name/artist")
            return None
    except Exception as e:
        logger.exception("Error fetching Spotify metadata: %s", e)
        return None


async def get_spotify_metadata(spotify_url: str) -> tuple[str | None, str | None]:
    """
    Return (title, artist_string) or (None, None) if not available.
    Uses Spotify Web API client credentials. Runs blocking network calls in thread.
    """
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        logger.warning("Spotify client id/secret not configured; skipping metadata fetch")
        return None, None

    track_id = extract_spotify_track_id(spotify_url)
    if not track_id:
        logger.warning("Could not parse track id from url for metadata: %s", spotify_url)
        return None, None

    try:
        token = await asyncio.to_thread(get_spotify_token_blocking, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        if not token:
            logger.error("Failed to obtain Spotify token for metadata")
            return None, None
        info = await asyncio.to_thread(get_spotify_track_info_blocking, token, track_id)
        if not info:
            logger.error("Spotify track info empty for metadata")
            return None, None
        title = info.get("name")
        artists = info.get("artists", [])
        artist_names = ", ".join(a.get("name") for a in artists if a.get("name"))
        logger.info("Spotify metadata fetched: title=%s artist=%s", title, artist_names)
        return title, artist_names
    except Exception as e:
        logger.exception("Error fetching Spotify metadata for metadata: %s", e)
        return None, None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    message = update.message.text.strip()
    logger.info("Received message from user %s: %s", user_id, message)

    if "spotify.com/track/" not in message:
        await update.message.reply_text("Please send a valid Spotify track URL.")
        logger.info("Invalid URL sent by user %s", user_id)
        return

    await update.message.reply_text("Downloading your song, please wait...")
    track_url = message

    # Fetch Spotify metadata early so we can use artist/title in tags and telegram fields
    title_meta, artist_meta = await get_spotify_metadata(track_url)
    logger.info("Metadata for track will be used: title=%s artist=%s", title_meta, artist_meta)

    with tempfile.TemporaryDirectory() as tmpdirname:
        logger.info("Created temporary directory: %s", tmpdirname)

        env = os.environ.copy()
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            env["SPOTIFY_CLIENT_ID"] = SPOTIFY_CLIENT_ID
            env["SPOTIFY_CLIENT_SECRET"] = SPOTIFY_CLIENT_SECRET
            logger.info("Using Spotify API credentials from environment")
        else:
            logger.warning("Spotify API credentials not set. Rate limiting may occur.")

        # Step 1: Attempt to get YouTube URL from spotdl url (capture stdout+stderr)
        spotdl_cmd = ["spotdl", "url", track_url]
        logger.info("Executing spotdl command: %s", spotdl_cmd)
        try:
            # allow_nonzero True so we can inspect stderr for URLs even if spotdl failed
            spotdl_output = await run_subprocess(spotdl_cmd, env=env, allow_nonzero=True)
            logger.info("spotdl raw output (stdout+stderr):\n%s", spotdl_output)
            yt_url = extract_youtube_url(spotdl_output)

            if yt_url:
                logger.info("Extracted YouTube URL from spotdl output: %s", yt_url)
            else:
                logger.info("No YouTube URL in spotdl output; trying Spotify metadata + yt-dlp search fallback.")
        except Exception as e:
            # run_subprocess shouldn't raise here because allow_nonzero=True, but just in case
            logger.exception("Error running spotdl url: %s", e)
            yt_url = None

        # If spotdl didn't give us a URL, fallback to searching YouTube via yt-dlp using Spotify metadata
        if not yt_url:
            search_query = await get_spotify_search_query(track_url)
            if not search_query:
                await update.message.reply_text("Failed to resolve the track to a YouTube URL and couldn't fetch Spotify metadata for fallback.")
                logger.error("No search query could be built for fallback.")
                return

            # Use yt-dlp search to download the first match
            yt_search_arg = f"ytsearch1:{search_query}"
            logger.info("Using yt-dlp search arg: %s", yt_search_arg)
            ytdlp_cmd = [
                "yt-dlp",
                "-f", "mp4",
                "-o", os.path.join(tmpdirname, "%(title)s.%(ext)s"),
                yt_search_arg
            ]
            try:
                # for download we want to raise on errors
                await run_subprocess(ytdlp_cmd, env=env, allow_nonzero=False)
            except Exception as e:
                await update.message.reply_text(f"yt-dlp search/download fallback failed: {e}")
                logger.error("yt-dlp search failed: %s", e)
                return

            # find downloaded file and continue to conversion/send
            media_file = None
            for root, _, files in os.walk(tmpdirname):
                for file in files:
                    if file.endswith(".mp4") or file.endswith(".mkv") or file.endswith(".m4a") or file.endswith(".webm"):
                        media_file = os.path.join(root, file)
                        logger.info("Found media file from ytsearch: %s", media_file)
                        break
                if media_file:
                    break

            if not media_file:
                await update.message.reply_text("yt-dlp search completed but no media file was found.")
                logger.error("No media file found in tempdir after yt-dlp search.")
                return

            # Convert to mp3 with metadata
            mp3_path = os.path.splitext(media_file)[0] + ".mp3"
            logger.info("Converting %s to %s (embedding metadata)", media_file, mp3_path)
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", media_file,
                "-vn", "-ab", "192k", "-ar", "44100"
            ]
            if title_meta:
                ffmpeg_cmd += ["-metadata", f"title={title_meta}"]
            if artist_meta:
                ffmpeg_cmd += ["-metadata", f"artist={artist_meta}"]
            ffmpeg_cmd += ["-f", "mp3", mp3_path]

            try:
                await run_subprocess(ffmpeg_cmd, env=env, allow_nonzero=False)
                logger.info("Conversion complete")
            except Exception as e:
                await update.message.reply_text(f"Error converting video to mp3: {e}")
                logger.error("ffmpeg conversion failed: %s", e)
                return

            # Send mp3 with Telegram metadata (performer/title)
            send_title = title_meta or os.path.splitext(os.path.basename(mp3_path))[0]
            send_artist = artist_meta or None
            try:
                with open(mp3_path, "rb") as audio_file:
                    await update.message.reply_audio(audio=audio_file, title=send_title, performer=send_artist)
                await update.message.reply_text("Here's your song! 🎶")
                logger.info("Sent audio file to user %s", user_id)
            except TimedOut as e:
                await update.message.reply_text(
                    "Error: Timed out while sending the song. The file may be too large or the network is slow."
                )
                logger.error("Timed out sending audio to user %s: %s", user_id, e)
            except Exception as e:
                await update.message.reply_text(f"Error sending the song: {e}")
                logger.error("Failed to send audio to user %s: %s", user_id, e)
            return

        # If we have a yt_url from spotdl, proceed to download it with yt-dlp (normalized already)
        yt_output_template = os.path.join(tmpdirname, "%(title)s.%(ext)s")
        ytdlp_cmd = [
            "yt-dlp",
            "-f", "mp4",
            "-o", yt_output_template,
            yt_url
        ]
        logger.info("Executing yt-dlp command: %s", ytdlp_cmd)
        try:
            await run_subprocess(ytdlp_cmd, env=env, allow_nonzero=False)
        except Exception as e:
            await update.message.reply_text(f"yt-dlp failed: {e}")
            logger.error("yt-dlp failed: %s", e)
            return

        # Step 3: Find downloaded media file (.mp4 or .mkv or .m4a)
        media_file = None
        for root, _, files in os.walk(tmpdirname):
            for file in files:
                if file.endswith(".mp4") or file.endswith(".mkv") or file.endswith(".webm") or file.endswith(".m4a"):
                    media_file = os.path.join(root, file)
                    logger.info("Found media file: %s", media_file)
                    break
            if media_file:
                break

        if not media_file:
            await update.message.reply_text("No video file was downloaded.")
            logger.warning("No media file found in %s", tmpdirname)
            return

        # Step 4: Convert video to mp3 embedding metadata (title/artist)
        mp3_path = os.path.splitext(media_file)[0] + ".mp3"
        logger.info("Converting %s to %s (embedding metadata)", media_file, mp3_path)
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", media_file,
            "-vn", "-ab", "192k", "-ar", "44100"
        ]
        if title_meta:
            ffmpeg_cmd += ["-metadata", f"title={title_meta}"]
        if artist_meta:
            ffmpeg_cmd += ["-metadata", f"artist={artist_meta}"]
        ffmpeg_cmd += ["-f", "mp3", mp3_path]

        try:
            await run_subprocess(ffmpeg_cmd, env=env, allow_nonzero=False)
            logger.info("Conversion complete")
        except Exception as e:
            await update.message.reply_text(f"Error converting video to mp3: {e}")
            logger.error("ffmpeg conversion failed: %s", e)
            return

        # Step 5: Send MP3 to user with performer/title fields
        send_title = title_meta or os.path.splitext(os.path.basename(mp3_path))[0]
        send_artist = artist_meta or None
        try:
            with open(mp3_path, "rb") as audio_file:
                await update.message.reply_audio(audio=audio_file, title=send_title, performer=send_artist)
            await update.message.reply_text("Here's your song! 🎶")
            logger.info("Sent audio file to user %s", user_id)
        except TimedOut as e:
            await update.message.reply_text(
                "Error: Timed out while sending the song. The file may be too large or the network is slow."
            )
            logger.error("Timed out sending audio to user %s: %s", user_id, e)
        except Exception as e:
            await update.message.reply_text(f"Error sending the song: {e}")
            logger.error("Failed to send audio to user %s: %s", user_id, e)


async def main():
    logger.info("Starting Spotify Telegram Bot...")
    application = Application.builder().token(TOKEN).read_timeout(60).write_timeout(60).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Handlers added. Starting polling...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)


if __name__ == "__main__":
    logger.info("Script execution started")
    asyncio.run(main())
