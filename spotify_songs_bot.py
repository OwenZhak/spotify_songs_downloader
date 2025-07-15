import subprocess
import os
import tempfile
import logging
import asyncio
import nest_asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TimedOut

# Apply nest_asyncio to handle nested event loops in VS Code
nest_asyncio.apply()

# Configure logging for debug output
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram bot token (replace with your actual token)
TOKEN = ""  # Replace with your bot's API token

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command with an inline button."""
    logger.info("Received /start command from user %s", update.message.from_user.id)
    keyboard = [
        [InlineKeyboardButton("Download a Song", callback_data='download_song')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to the Spotify Song Downloader Bot! Click the button to start downloading a song.",
        reply_markup=reply_markup
    )
    logger.info("Sent inline keyboard to user %s", update.message.from_user.id)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()
    logger.info("Button clicked by user %s: %s", query.from_user.id, query.data)
    if query.data == 'download_song':
        await query.message.reply_text("Send a Spotify link to a song.")
        logger.info("Prompted user %s to send a Spotify link", query.from_user.id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages with Spotify URLs."""
    logger.info("Received message from user %s: %s", update.message.from_user.id, update.message.text)
    message = update.message.text
    if "spotify.com/track/" in message:
        logger.info("Detected Spotify track URL: %s", message)
        await update.message.reply_text("Downloading your song, please wait...")
        track_url = message.strip()
        
        with tempfile.TemporaryDirectory() as tmpdirname:
            logger.info("Created temporary directory: %s", tmpdirname)
            command = ["spotdl", "--output", tmpdirname, track_url]
            logger.info("Executing command: %s", command)
            try:
                subprocess.run(command, check=True)
                
                mp3_file = None
                for root, dirs, files in os.walk(tmpdirname):
                    for file in files:
                        if file.endswith(".mp3"):
                            mp3_file = os.path.join(root, file)
                            logger.info("Found MP3 file: %s", mp3_file)
                            break
                
                if mp3_file:
                    try:
                        with open(mp3_file, 'rb') as audio_file:
                            await update.message.reply_audio(audio=audio_file)
                        await update.message.reply_text("Here's your song! 🎶")
                        logger.info("Sent MP3 to user %s", update.message.from_user.id)
                    except TimedOut as e:
                        await update.message.reply_text("Error: Timed out while sending the song. The file may be too large or the network is slow.")
                        logger.error("Timed out sending MP3 to user %s: %s", update.message.from_user.id, e)
                    except Exception as e:
                        await update.message.reply_text(f"Error sending the song: {e}")
                        logger.error("Failed to send MP3 to user %s: %s", update.message.from_user.id, e)
                else:
                    await update.message.reply_text("No song was downloaded. The link might be invalid.")
                    logger.warning("No MP3 file found in %s", tmpdirname)
            
            except subprocess.CalledProcessError as e:
                await update.message.reply_text(f"Error downloading the song: {e}")
                logger.error("Download failed: %s", e)
    else:
        await update.message.reply_text("Please send a valid Spotify track URL.")
        logger.info("Invalid URL sent by user %s", update.message.from_user.id)

async def main():
    logger.info("Starting Spotify Telegram Bot...")
    # Increase HTTP timeout to 60 seconds
    application = Application.builder().token(TOKEN).read_timeout(60).write_timeout(60).build()
    logger.info("Bot application initialized")
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Handlers added")
    
    # Start polling with a 1-second interval
    logger.info("Starting polling...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)

if __name__ == "__main__":
    logger.info("Script execution started")
    asyncio.run(main())