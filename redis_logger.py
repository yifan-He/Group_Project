import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis


class RedisLogger:
    ROUTES = ("food", "path", "chat")
    LOCAL_ARCHIVE_DIRNAME = "redis_log_archive"
    LOCAL_RETENTION_DAYS = 7
    DEFAULT_SESSION_TTL_SECONDS = 3600

    def __init__(self, config):
        decode_responses = (
            str(
                os.getenv("REDIS_DECODE_RESPONSES")
                or config.get("REDIS", "DECODE_RESPONSES", fallback="True")
            ).lower()
            == "true"
        )
        
        # Read redis configuration values from the ini file
        self.redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST") or config.get("REDIS", "HOST", fallback=""),
            port=int(os.getenv("REDIS_PORT") or config.get("REDIS", "PORT", fallback="6379")),
            username=os.getenv("REDIS_USERNAME") or config.get("REDIS", "USERNAME", fallback=""),
            password=os.getenv("REDIS_PASSWORD") or config.get("REDIS", "PASSWORD", fallback=""),
            decode_responses=decode_responses,
        )
        self.instance_id = (
            os.getenv("BOT_INSTANCE_ID") or config.get("BOT", "INSTANCE_ID", fallback="")
        ).strip() or None
        self.session_ttl_seconds = self._to_int(
            os.getenv("BOT_SESSION_TTL_SECONDS")
            or config.get(
                "BOT",
                "SESSION_TTL_SECONDS",
                fallback=str(self.DEFAULT_SESSION_TTL_SECONDS),
            )
        ) or self.DEFAULT_SESSION_TTL_SECONDS
        archive_root = Path(__file__).resolve().parent / self.LOCAL_ARCHIVE_DIRNAME
        self.archive_dir = archive_root / self.instance_id if self.instance_id else archive_root
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_local_archives()

        # Fail fast when Redis is unreachable so issues are visible at startup.
        self.redis_client.ping()

    @staticmethod
    def _now_str():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _now_iso():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _route_count_key(route_name):
        return f"metrics:route:{route_name}:count"

    @staticmethod
    def _route_error_key(route_name):
        return f"metrics:route:{route_name}:errors"

    @staticmethod
    def _session_key(user_id, chat_id):
        return f"session:chat:{chat_id}:user:{user_id}"

    @staticmethod
    def _json_loads_safe(value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {"raw": value}

    def is_healthy(self):
        try:
            return bool(self.redis_client.ping())
        except redis.RedisError:
            return False

    def cleanup_local_archives(self, retention_days=None):
        retention_days = retention_days or self.LOCAL_RETENTION_DAYS
        cutoff = datetime.now() - timedelta(days=retention_days)

        for file_path in self.archive_dir.iterdir():
            if not file_path.is_file() or file_path.name == ".gitkeep":
                continue

            try:
                modified_at = datetime.fromtimestamp(file_path.stat().st_mtime)
            except OSError:
                continue

            if modified_at < cutoff:
                try:
                    file_path.unlink()
                except OSError:
                    continue

    def _append_local_archive_entry(self, prefix, payload):
        file_path = self.archive_dir / f"{prefix}_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        record = dict(payload)
        record.setdefault("archived_at", self._now_iso())
        if self.instance_id:
            record.setdefault("instance_id", self.instance_id)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return str(file_path)

    def export_run_archive(self, reason="shutdown"):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        today = datetime.now().strftime("%Y-%m-%d")
        system_log_key = f"system:log_{today}"
        system_logs = [
            self._json_loads_safe(item)
            for item in self.redis_client.lrange(system_log_key, 0, -1)
        ]

        chat_logs = {}
        chat_log_keys = sorted(
            str(key.decode("utf-8") if isinstance(key, bytes) else key)
            for key in self.redis_client.scan_iter(match="user:*_chat_logs")
        )
        for log_key in chat_log_keys:
            chat_logs[log_key] = [
                self._json_loads_safe(item)
                for item in self.redis_client.lrange(log_key, 0, -1)
            ]

        snapshot = {
            "instance_id": self.instance_id,
            "reason": reason,
            "exported_at": self._now_iso(),
            "system_log_key": system_log_key,
            "system_logs": system_logs,
            "chat_logs": chat_logs,
            "metrics_snapshot": self.get_metrics_snapshot(),
        }

        file_path = self.archive_dir / f"redis_run_snapshot_{timestamp}.json"
        with file_path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        return str(file_path)

    # Save a system log
    def save_system_log(self, level, message):

        today = datetime.now().strftime("%Y-%m-%d")
        log_key = f"system:log_{today}"

        log = {
            "time": self._now_str(),
            "level": level,  # INFO / ERROR / WARNING
            "message": message,
        }
        if self.instance_id:
            log["instance_id"] = self.instance_id

        log_json = json.dumps(log, ensure_ascii=False)

        self.redis_client.rpush(log_key, log_json)
        self._append_local_archive_entry(
            "system_log",
            {
                "redis_key": log_key,
                **log,
            },
        )
        
        # Keep only the latest 7 days of system logs to prevent unbounded growth
        days_ago = 7
        old_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        old_key = f"system:log_{old_date}"
        self.redis_client.delete(old_key)

    # Save the chat log to Redis 
    def save_chat_log(self, user_id, user_msg, bot_reply):

        log_key = f"user:{user_id}_chat_logs"

        log = {
            "time": self._now_str(),
            "user_msg": user_msg,
            "bot_reply": bot_reply,
        }
        if self.instance_id:
            log["instance_id"] = self.instance_id

        log_json = json.dumps(log, ensure_ascii=False)

        self.redis_client.rpush(log_key, log_json)
        self._append_local_archive_entry(
            "chat_log",
            {
                "redis_key": log_key,
                "user_id": user_id,
                **log,
            },
        )

        # Keep only the latest 50 logs to prevent unbounded growth
        self.redis_client.ltrim(log_key, -50, -1)

    def get_user_state(self, user_id, chat_id):
        raw_state = self.redis_client.get(self._session_key(user_id, chat_id))
        if not raw_state:
            return {}

        state = self._json_loads_safe(raw_state)
        return state if isinstance(state, dict) else {}

    def set_user_state(self, user_id, chat_id, state, ttl_seconds=None):
        if not state:
            self.clear_user_state(user_id, chat_id)
            return

        ttl_seconds = self._to_int(ttl_seconds) or self.session_ttl_seconds
        self.redis_client.set(
            self._session_key(user_id, chat_id),
            json.dumps(state, ensure_ascii=False),
            ex=max(1, ttl_seconds),
        )

    def clear_user_state(self, user_id, chat_id):
        self.redis_client.delete(self._session_key(user_id, chat_id))

    def record_request(self, route_name):
        pipeline = self.redis_client.pipeline()
        pipeline.incr("metrics:requests:total")
        pipeline.incr("metrics:llm:calls:total")
        pipeline.incr(self._route_count_key(route_name))
        pipeline.set("metrics:llm:last_request_at", self._now_iso())
        pipeline.execute()

    def record_success(self, route_name, elapsed_ms, usage=None, usage_source=None):
        del route_name
        usage = usage or {}
        prompt_tokens = self._to_int(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        )
        completion_tokens = self._to_int(
            usage.get("completion_tokens") or usage.get("output_tokens")
        )
        total_tokens = self._to_int(usage.get("total_tokens"))
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        pipeline = self.redis_client.pipeline()
        pipeline.hincrby("metrics:llm:lat", "sum_ms", max(0, int(elapsed_ms)))
        pipeline.hincrby("metrics:llm:lat", "n", 1)
        pipeline.set("metrics:llm:last_success_at", self._now_iso())
        if usage_source:
            pipeline.set("metrics:llm:last_usage_source", usage_source)
        if prompt_tokens:
            pipeline.hincrby("metrics:llm:tokens", "prompt", prompt_tokens)
        if completion_tokens:
            pipeline.hincrby("metrics:llm:tokens", "completion", completion_tokens)
        if total_tokens:
            pipeline.hincrby("metrics:llm:tokens", "total", total_tokens)
        pipeline.execute()

    def record_error(self, route_name, error_message):
        error_payload = json.dumps(
            {
                "time": self._now_iso(),
                "route": route_name,
                "error": error_message,
                "instance_id": self.instance_id,
            },
            ensure_ascii=False,
        )

        pipeline = self.redis_client.pipeline()
        pipeline.incr("metrics:errors:total")
        pipeline.incr(self._route_error_key(route_name))
        pipeline.set("metrics:llm:last_error", error_payload)
        pipeline.execute()

        self.save_system_log("ERROR", f"LLM {route_name} error: {error_message}")

    def get_metrics_snapshot(self):
        scalar_keys = [
            "metrics:requests:total",
            "metrics:errors:total",
            "metrics:llm:calls:total",
            "metrics:llm:last_request_at",
            "metrics:llm:last_success_at",
            "metrics:llm:last_usage_source",
            "metrics:llm:last_error",
        ]
        scalar_values = self.redis_client.mget(scalar_keys)
        scalar_metrics = dict(zip(scalar_keys, scalar_values))

        latency_metrics = self.redis_client.hgetall("metrics:llm:lat")
        token_metrics = self.redis_client.hgetall("metrics:llm:tokens")

        route_counts = {}
        route_errors = {}
        for route_name in self.ROUTES:
            count_value, error_value = self.redis_client.mget(
                [
                    self._route_count_key(route_name),
                    self._route_error_key(route_name),
                ]
            )
            route_counts[route_name] = self._to_int(count_value)
            route_errors[route_name] = self._to_int(error_value)

        successful_calls = self._to_int(latency_metrics.get("n"))
        total_latency_ms = self._to_float(latency_metrics.get("sum_ms"))
        average_latency_ms = (
            round(total_latency_ms / successful_calls, 2)
            if successful_calls
            else None
        )

        prompt_tokens = self._to_int(token_metrics.get("prompt"))
        completion_tokens = self._to_int(token_metrics.get("completion"))
        total_tokens = self._to_int(token_metrics.get("total"))
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        last_error = scalar_metrics.get("metrics:llm:last_error")
        if last_error:
            try:
                last_error = json.loads(last_error)
            except json.JSONDecodeError:
                last_error = {"error": last_error}

        return {
            "redis_ok": self.is_healthy(),
            "requests_total": self._to_int(scalar_metrics.get("metrics:requests:total")),
            "errors_total": self._to_int(scalar_metrics.get("metrics:errors:total")),
            "llm_calls_total": self._to_int(
                scalar_metrics.get("metrics:llm:calls:total")
            ),
            "average_latency_ms": average_latency_ms,
            "last_request_at": scalar_metrics.get("metrics:llm:last_request_at"),
            "last_success_at": scalar_metrics.get("metrics:llm:last_success_at"),
            "usage_source": scalar_metrics.get("metrics:llm:last_usage_source"),
            "last_error": last_error,
            "route_counts": route_counts,
            "route_errors": route_errors,
            "token_totals": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": total_tokens,
            },
        }
