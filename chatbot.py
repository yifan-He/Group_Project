"""
This program requires the following modules:
- python-telegram-bot==22.5
- urllib3==2.6.2
"""

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
import configparser
import logging
from ChatGPT_HKBU import ChatGPT
from redis_logger import RedisLogger

gpt = None
redis_logger = None

def main():
    
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    # Create a Redis logger object
    global redis_logger
    redis_logger = RedisLogger(config)
    
    # Create a ChatGPT client object
    global gpt
    gpt = ChatGPT(config)
    
    # Configure logging so you can see initialization and error messages
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # Load the configuration data from file and save a startup log entry in Redis
    logging.info("INIT: Loading configuration...")
    redis_logger.save_system_log("INFO", "INIT: Loading configuration...")
    


    # Create an Application for your bot
    logging.info("INIT: Connecting the Telegram bot...")
    redis_logger.save_system_log("INFO", "INIT: Connecting the Telegram bot...")
    app = ApplicationBuilder().token(config["TELEGRAM"]["ACCESS_TOKEN"]).build()

    # Register message handlers
    logging.info("INIT: Registering the message handler...")
    redis_logger.save_system_log("INFO", "INIT: Registering the message handler...")
    app.add_handler(MessageHandler(filters.LOCATION, location_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, callback))

    # Register command handlers
    logging.info("INIT: Registering the command handlers...")
    redis_logger.save_system_log("INFO", "INIT: Registering the command handlers...")
    app.add_handler(CommandHandler("start", start_callback))
    app.add_handler(CommandHandler("food", food_callback))
    app.add_handler(CommandHandler("path", path_callback))

    # Start the bot
    logging.info("INIT: Initialization done!")
    redis_logger.save_system_log("INFO", "INIT: Initialization done!")
    app.run_polling()

def guide_message(user_name: str) -> str:
    return f"""👋 Hi {user_name}！Welcome to use the Travel Assistance～

*------------Quick Commands------------*
/start → Show this guide again
/food → View the top 5 recommended restaurants nearby
/path → View the fastest route paths from your location to the destination

*---------------Direct Chat---------------*
You can also chat with me directly! Please don't just say Hi. 

Hope you have a wonderful trip! ❤

"""


# /start → display the guide message
async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_name = update.effective_user.first_name

    guide_msg = guide_message(user_name)

    await update.message.reply_text(guide_msg)


# /food → recommend the top 5 restaurants nearby
async def food_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "food_wait_location"
    await update.message.reply_text(
        "Please send your current location."
    )


# /path → ask the user for the destination and then provide the fastest route path
async def path_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "path_wait_location"
    context.user_data["path_location"] = None
    await update.message.reply_text(
        "Please send your current location."
    )

# handle location messages
async def location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    location = update.message.location
    latitude = location.latitude
    longitude = location.longitude

    # ask for restaurant recommendations after receiving location for /food
    if mode == "food_wait_location":
        context.user_data["mode"] = None

        loading_msg = await update.message.reply_text(
            "Finding the top 5 restaurants nearby for you..."
        )

        prompt = f"""
You are a travel concierge.
My current location is latitude {latitude} and longitude {longitude}.
Please recommend the TOP 5 restaurants near this location, with short descriptions.
Output format:
1. Restaurant name - Rating - Short description
2. ...
Keep it clear and simple.
"""

        response = gpt.submit(prompt)
        await loading_msg.edit_text(f"The Top 5 Restaurants Recommended:\n\n{response}")

        redis_logger.save_chat_log(
            user_id=update.effective_user.id,
            user_msg="/food (Get top 5 restaurants with live location)",
            bot_reply=response,
        )
        return

    # ask for destination after receiving location for /path
    if mode == "path_wait_location":
        context.user_data["path_location"] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        context.user_data["mode"] = "path_wait_destination"
        await update.message.reply_text(
            "Location received. Now please tell me your DESTINATION.\nExample: Hong Kong Airport"
        )
        return

    await update.message.reply_text("Location received. Please run /food or /path first.")

# handle text messages
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # await update.message.reply_text(response)
    logging.info("UPDATE: " + str(update))
    loading_message = await update.message.reply_text("Thinking...")

    # Get the user's message
    user_msg = update.message.text

    # Check if we are waiting for the destination after receiving location for /path
    if context.user_data.get("mode") == "path_wait_destination":
        destination = (user_msg or "").strip()
        path_location = context.user_data.get("path_location")

        if not destination:
            await loading_message.edit_text("Destination is empty. Please send your destination.")
            return

        if not path_location:
            context.user_data["mode"] = None
            await loading_message.edit_text("Location is missing. Please run /path again.")
            return

        context.user_data["mode"] = None
        context.user_data["path_location"] = None

        prompt = f"""
You are a travel concierge.
My current location is latitude {path_location['latitude']} and longitude {path_location['longitude']}.
Tell me the FASTEST route from this location to {destination}.
Include:
- Transport method
- Estimated time
- Simple direction

Keep it clear and short.
"""

        response = gpt.submit(prompt)
        await loading_message.edit_text(response)

        redis_logger.save_chat_log(
            user_id=update.effective_user.id,
            user_msg=f"/path to {destination} (with live location)",
            bot_reply=response,
        )
        return

    prompt = user_msg

    # submit the prompt to ChatGPT
    response = gpt.submit(prompt)

    # send the response
    await loading_message.edit_text(response)

    # log the conversation
    redis_logger.save_chat_log(
        user_id=update.effective_user.id,
        user_msg=update.message.text,
        bot_reply=response,
    )

if __name__ == "__main__":
    main()
