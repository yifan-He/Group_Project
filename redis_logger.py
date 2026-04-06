import json
import redis
from datetime import datetime, timedelta


class RedisLogger:
    def __init__(self, config):
        decode_responses = (
            str(config["REDIS"].get("DECODE_RESPONSES", "True")).lower() == "true"
        )
        
        # Read redis configuration values from the ini file
        self.redis_client = redis.Redis(
            host=config["REDIS"]["HOST"],
            port=int(config["REDIS"]["PORT"]),
            username=config["REDIS"]["USERNAME"],
            password=config["REDIS"]["PASSWORD"],
            decode_responses=decode_responses,
        )

        # Fail fast when Redis is unreachable so issues are visible at startup.
        self.redis_client.ping()

    # Save a system log
    def save_system_log(self, level, message):

        today = datetime.now().strftime("%Y-%m-%d")
        log_key = f"system:log_{today}"

        log = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,  # INFO / ERROR / WARNING
            "message": message,
        }

        log_json = json.dumps(log, ensure_ascii=False)

        self.redis_client.rpush(log_key, log_json)
        
        # Keep only the latest 7 days of system logs to prevent unbounded growth
        days_ago = 7
        old_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        old_key = f"system:log_{old_date}"
        self.redis_client.delete(old_key)

    # Save the chat log to Redis 
    def save_chat_log(self, user_id, user_msg, bot_reply):

        log_key = f"user:{user_id}_chat_logs"

        log = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_msg": user_msg,
            "bot_reply": bot_reply,
        }

        log_json = json.dumps(log, ensure_ascii=False)

        self.redis_client.rpush(log_key, log_json)

        # Keep only the latest 50 logs to prevent unbounded growth
        self.redis_client.ltrim(log_key, -50, -1)
