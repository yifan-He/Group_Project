import asyncio
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update


LOGGER = logging.getLogger(__name__)


def _normalize_path(path_value):
    path_value = str(path_value or "").strip()
    if not path_value:
        return "/"
    return path_value if path_value.startswith("/") else f"/{path_value}"


@dataclass
class WebhookRuntimeConfig:
    listen: str = "0.0.0.0"
    port: int = 8080
    webhook_path: str = "/telegram-webhook"
    health_path: str = "/healthz"
    instance_id: str = "bot"
    webhook_secret: str | None = None
    register_webhook: bool = False
    external_webhook_url: str | None = None

    def __post_init__(self):
        self.webhook_path = _normalize_path(self.webhook_path)
        self.health_path = _normalize_path(self.health_path)


class _ApplicationRunner:
    def __init__(self, application, runtime_config: WebhookRuntimeConfig):
        self.application = application
        self.runtime_config = runtime_config
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop,
            name=f"ptb-{runtime_config.instance_id}",
            daemon=True,
        )
        self._started = threading.Event()
        self._startup_error = None

    async def _startup(self):
        await self.application.initialize()

        if self.runtime_config.register_webhook:
            if not self.runtime_config.external_webhook_url:
                raise RuntimeError(
                    "external_webhook_url is required when register_webhook is enabled."
                )

            await self.application.bot.set_webhook(
                url=self.runtime_config.external_webhook_url,
                secret_token=self.runtime_config.webhook_secret,
            )
            LOGGER.info(
                "WEBHOOK: Registered Telegram webhook for %s at %s",
                self.runtime_config.instance_id,
                self.runtime_config.external_webhook_url,
            )

        await self.application.start()

    async def _shutdown(self):
        await self.application.stop()
        await self.application.shutdown()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._startup())
            self._started.set()
            self.loop.run_forever()
        except Exception as exc:
            self._startup_error = exc
            self._started.set()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def start(self, timeout_seconds=30):
        self.thread.start()
        self._started.wait(timeout_seconds)
        if self._startup_error:
            raise self._startup_error
        if not self._started.is_set():
            raise RuntimeError("Webhook application startup timed out.")

    def stop(self, timeout_seconds=30):
        if not self.thread.is_alive():
            return

        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            future.result(timeout=timeout_seconds)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=timeout_seconds)


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, application, runtime_config, app_loop):
        super().__init__(server_address, handler_cls)
        self.application = application
        self.runtime_config = runtime_config
        self.app_loop = app_loop


class _WebhookRequestHandler(BaseHTTPRequestHandler):
    server_version = "ChatbotWebhook/1.0"

    def log_message(self, message, *args):
        LOGGER.info(
            "WEBHOOK %s - %s",
            self.server.runtime_config.instance_id,
            message % args,
        )

    def _write_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_secret(self):
        expected_secret = self.server.runtime_config.webhook_secret
        if not expected_secret:
            return True

        actual_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        return actual_secret == expected_secret

    def do_GET(self):
        if self.path != self.server.runtime_config.health_path:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "Not Found"},
            )
            return

        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "instance_id": self.server.runtime_config.instance_id,
                "mode": "webhook",
            },
        )

    def do_POST(self):
        if self.path != self.server.runtime_config.webhook_path:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "Not Found"},
            )
            return

        if not self._validate_secret():
            self._write_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "Invalid secret token"},
            )
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
            update = Update.de_json(payload, bot=self.server.application.bot)
            future = asyncio.run_coroutine_threadsafe(
                self.server.application.update_queue.put(update),
                self.server.app_loop,
            )
            future.result(timeout=5)
        except Exception as exc:
            LOGGER.exception(
                "WEBHOOK %s - Failed to enqueue update",
                self.server.runtime_config.instance_id,
            )
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "instance_id": self.server.runtime_config.instance_id,
                    "error": str(exc),
                },
            )
            return

        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "instance_id": self.server.runtime_config.instance_id,
                "queued": True,
            },
        )


def run_local_webhook_application(application, runtime_config: WebhookRuntimeConfig):
    app_runner = _ApplicationRunner(application, runtime_config)
    app_runner.start()

    http_server = _WebhookHTTPServer(
        (runtime_config.listen, runtime_config.port),
        _WebhookRequestHandler,
        application,
        runtime_config,
        app_runner.loop,
    )
    server_thread = threading.Thread(
        target=http_server.serve_forever,
        name=f"http-{runtime_config.instance_id}",
        daemon=True,
    )
    server_thread.start()

    LOGGER.info(
        "WEBHOOK: Instance %s listening on http://%s:%s%s",
        runtime_config.instance_id,
        runtime_config.listen,
        runtime_config.port,
        runtime_config.webhook_path,
    )
    LOGGER.info(
        "WEBHOOK: Instance %s health endpoint on http://%s:%s%s",
        runtime_config.instance_id,
        runtime_config.listen,
        runtime_config.port,
        runtime_config.health_path,
    )

    stop_event = threading.Event()
    previous_handlers = {}

    def _request_stop(signum, _frame):
        LOGGER.info(
            "WEBHOOK: Received signal %s for instance %s",
            signum,
            runtime_config.instance_id,
        )
        stop_event.set()

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[handled_signal] = signal.getsignal(handled_signal)
            signal.signal(handled_signal, _request_stop)
        except (AttributeError, ValueError):
            continue

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            try:
                signal.signal(handled_signal, previous_handler)
            except (AttributeError, ValueError):
                continue

        http_server.shutdown()
        http_server.server_close()
        server_thread.join(timeout=10)
        app_runner.stop()
