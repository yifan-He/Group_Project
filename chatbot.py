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
import os
import time
from datetime import datetime, timezone
from ChatGPT_HKBU import ChatGPT
from redis_logger import RedisLogger

gpt = None
redis_logger = None
app_started_at = None


def _safe_metric_call(method_name, *args):
    if redis_logger is None:
        return None

    try:
        method = getattr(redis_logger, method_name)
        return method(*args)
    except Exception as exc:
        logging.warning("METRIC %s failed: %s", method_name, exc)
        return None


def get_metrics_snapshot():
    snapshot = {
        "redis_ok": False,
        "requests_total": 0,
        "errors_total": 0,
        "llm_calls_total": 0,
        "average_latency_ms": None,
        "last_request_at": None,
        "last_success_at": None,
        "usage_source": None,
        "last_error": None,
        "route_counts": {"food": 0, "path": 0, "chat": 0},
        "route_errors": {"food": 0, "path": 0, "chat": 0},
        "token_totals": {"prompt": 0, "completion": 0, "total": 0},
    }

    if redis_logger is None:
        return snapshot

    try:
        snapshot.update(redis_logger.get_metrics_snapshot())
    except Exception as exc:
        logging.warning("Unable to load metrics snapshot: %s", exc)
    return snapshot


def format_uptime():
    if app_started_at is None:
        return "Unknown"

    elapsed_seconds = int((datetime.now(timezone.utc) - app_started_at).total_seconds())
    days, remainder = divmod(elapsed_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def estimate_cost(snapshot):
    if gpt is None:
        return None, "The LLM client is not initialized yet."

    token_totals = snapshot["token_totals"]
    prompt_tokens = token_totals["prompt"]
    completion_tokens = token_totals["completion"]
    total_tokens = token_totals["total"]
    usage_source = snapshot.get("usage_source")

    if gpt.input_price_per_1k is not None and gpt.output_price_per_1k is not None:
        estimated_cost = (
            (prompt_tokens / 1000.0) * gpt.input_price_per_1k
            + (completion_tokens / 1000.0) * gpt.output_price_per_1k
        )
        source_note = (
            "using API-reported token usage."
            if usage_source == "api"
            else "using locally estimated token usage."
        )
        return round(estimated_cost, 6), (
            "Estimated from configured input and output token pricing, "
            + source_note
        )

    if gpt.total_price_per_1k is not None and total_tokens:
        estimated_cost = (total_tokens / 1000.0) * gpt.total_price_per_1k
        source_note = (
            "using API-reported token usage."
            if usage_source == "api"
            else "using locally estimated token usage."
        )
        return round(estimated_cost, 6), (
            "Estimated from configured blended token pricing, " + source_note
        )

    if total_tokens:
        if usage_source == "api":
            return None, "Token usage is available from the API, but no pricing is configured."
        return None, "Token usage is available from a local estimate, but no pricing is configured."

    if snapshot["llm_calls_total"]:
        return None, "The API did not return token usage yet, so only call counts are available."

    return None, "No LLM usage has been recorded yet."


def build_status_message():
    snapshot = get_metrics_snapshot()
    route_counts = snapshot["route_counts"]
    route_errors = snapshot["route_errors"]
    last_error = snapshot.get("last_error") or {}

    lines = [
        "Bot status",
        f"Started at (UTC): {app_started_at.isoformat() if app_started_at else 'Unknown'}",
        f"Uptime: {format_uptime()}",
        f"Redis health: {'OK' if snapshot['redis_ok'] else 'UNAVAILABLE'}",
        f"LLM requests: {snapshot['requests_total']}",
        f"LLM errors: {snapshot['errors_total']}",
        (
            f"Average latency: {snapshot['average_latency_ms']} ms"
            if snapshot["average_latency_ms"] is not None
            else "Average latency: N/A"
        ),
        f"Last success: {snapshot['last_success_at'] or 'N/A'}",
        (
            "Route counts: "
            f"food={route_counts['food']}, path={route_counts['path']}, chat={route_counts['chat']}"
        ),
        (
            "Route errors: "
            f"food={route_errors['food']}, path={route_errors['path']}, chat={route_errors['chat']}"
        ),
    ]

    if last_error:
        error_route = last_error.get("route", "unknown")
        error_message = str(last_error.get("error", "unknown"))
        lines.append(f"Last error: {error_route} - {error_message[:120]}")

    return "\n".join(lines)


def build_cost_message():
    snapshot = get_metrics_snapshot()
    token_totals = snapshot["token_totals"]
    estimated_cost, cost_note = estimate_cost(snapshot)

    lines = [
        "Cost estimate",
        f"Model: {gpt.model if gpt else 'Unknown'}",
        f"LLM calls: {snapshot['llm_calls_total']}",
        f"Prompt tokens: {token_totals['prompt']}",
        f"Completion tokens: {token_totals['completion']}",
        f"Total tokens: {token_totals['total']}",
    ]

    if estimated_cost is not None:
        lines.append(f"Estimated LLM cost: USD {estimated_cost:.6f}")
    else:
        lines.append("Estimated LLM cost: unavailable")

    lines.append(f"Note: {cost_note}")
    lines.append("Note: this is a local usage estimate, not a cloud billing value.")
    return "\n".join(lines)


def submit_and_track(route_name, prompt):
    _safe_metric_call("record_request", route_name)
    started = time.perf_counter()

    try:
        response = gpt.submit_with_metadata(prompt)
    except Exception as exc:
        logging.exception("Unexpected LLM failure on route %s", route_name)
        _safe_metric_call("record_error", route_name, str(exc))
        return "Error: unexpected error while contacting the LLM."

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if response.get("ok"):
        _safe_metric_call(
            "record_success",
            route_name,
            elapsed_ms,
            response.get("usage"),
            response.get("usage_source"),
        )
        return response["content"]

    error_text = response.get("error") or "Unknown error"
    logging.error("LLM request failed on route %s: %s", route_name, error_text)
    _safe_metric_call("record_error", route_name, error_text)
    return response["content"]

def main():
    global app_started_at
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    # Create a Redis logger object
    global redis_logger
    redis_logger = RedisLogger(config)
    
    # Create a ChatGPT client object
    global gpt
    gpt = ChatGPT(config)
    app_started_at = datetime.now(timezone.utc)
    
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
    telegram_token = os.getenv("TELEGRAM_TOKEN") or config.get("TELEGRAM", "ACCESS_TOKEN", fallback="")
    app = ApplicationBuilder().token(telegram_token).build()

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
    app.add_handler(CommandHandler("status", status_callback))
    app.add_handler(CommandHandler("cost", cost_callback))

    # Start the bot
    logging.info("INIT: Initialization done!")
    redis_logger.save_system_log("INFO", "INIT: Initialization done!")
    archive_path = None
    try:
        app.run_polling()
    finally:
        archive_path = _safe_metric_call("export_run_archive", "run_polling_exit")
        if archive_path:
            logging.info("ARCHIVE: Redis snapshot exported to %s", archive_path)

# Generate the guide message content for /start command
def guide_message(user_name: str) -> str:
    return f"""👋 Hi {user_name}！Welcome to use the Travel Assistance～

*------------Quick Commands------------*
/start → Show this guide again
/food → View the top 5 recommended restaurants nearby
/path → View the fastest route paths from your location to the destination
/status → View the bot runtime health and request metrics
/cost → View the current local LLM usage and cost estimate

*---------------Direct Chat---------------*
You can also chat with me directly! Please don't just say Hi. 

Hope you have a wonderful trip! ❤

"""


# /start → display the guide message
async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_name = update.effective_user.first_name

    guide_msg = guide_message(user_name)

    await update.message.reply_text(guide_msg)


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    del context
    await update.message.reply_text(build_status_message())


async def cost_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    del context
    await update.message.reply_text(build_cost_message())


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

# Handle location messages
async def location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    location = update.message.location
    latitude = location.latitude
    longitude = location.longitude

    # Ask for restaurant recommendations after receiving location for /food
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

        response = submit_and_track("food", prompt)
        await loading_msg.edit_text(f"The Top 5 Restaurants Recommended:\n\n{response}")

        redis_logger.save_chat_log(
            user_id=update.effective_user.id,
            user_msg="/food (Get top 5 restaurants with live location)",
            bot_reply=response,
        )
        return

    # Ask for destination after receiving location for /path
    if mode == "path_wait_location":
        context.user_data["path_location"] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        context.user_data["mode"] = "path_wait_destination"
        await update.message.reply_text(
            "Location received. Now please tell me your destination.\nExample: Hong Kong Airport"
        )
        return

    await update.message.reply_text("Location received. Please run /food or /path first.")

# Handle text messages
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

        response = submit_and_track("path", prompt)
        await loading_message.edit_text(response)

        redis_logger.save_chat_log(
            user_id=update.effective_user.id,
            user_msg=f"/path to {destination} (with live location)",
            bot_reply=response,
        )
        return

    prompt = user_msg

    # Submit the prompt to ChatGPT
    response = submit_and_track("chat", prompt)

    # Send the response
    await loading_message.edit_text(response)

    # Log the conversation
    redis_logger.save_chat_log(
        user_id=update.effective_user.id,
        user_msg=update.message.text,
        bot_reply=response,
    )

if __name__ == "__main__":
    main()
