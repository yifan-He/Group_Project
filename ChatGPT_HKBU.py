import os

import requests

# A simple client for the ChatGPT REST API
class ChatGPT:
    def __init__(self, config):
        # Read API configuration values from the ini file
        api_key = os.getenv("CHATGPT_KEY") or config.get("CHATGPT", "API_KEY", fallback="")
        base_url = os.getenv("CHATGPT_URL") or config.get("CHATGPT", "BASE_URL", fallback="")
        model = os.getenv("CHATGPT_MODEL") or config.get("CHATGPT", "MODEL", fallback="")
        api_ver = os.getenv("CHATGPT_VER") or config.get("CHATGPT", "API_VER", fallback="")
        self.model = model
        self.max_tokens = int(
            os.getenv("CHATGPT_MAX_TOKENS")
            or config.get("CHATGPT", "MAX_TOKENS", fallback="110")
        )
        self.timeout_seconds = int(
            os.getenv("CHATGPT_TIMEOUT_SECONDS")
            or config.get("CHATGPT", "TIMEOUT_SECONDS", fallback="30")
        )
        self.total_price_per_1k = self._get_optional_float(
            config, "CHATGPT", "PRICE_PER_1K", "CHATGPT_PRICE_PER_1K"
        )
        self.input_price_per_1k = self._get_optional_float(
            config, "CHATGPT", "INPUT_PRICE_PER_1K", "CHATGPT_INPUT_PRICE_PER_1K"
        )
        self.output_price_per_1k = self._get_optional_float(
            config, "CHATGPT", "OUTPUT_PRICE_PER_1K", "CHATGPT_OUTPUT_PRICE_PER_1K"
        )

        # Construct the full REST endpoint URL for chat completions
        self.url = f'{base_url}/deployments/{model}/chat/completions?api-version={api_ver}'

        # Set HTTP headers required for authentication and JSON payload
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "api-key": api_key,
        }

        # Define the system prompt to guide the assistant’s behavior
        self.system_message = (
            'You are a travel helper for university students. '
            'Be concise, direct, and practical. '
            'Use simple language and keep responses to 3-6 short lines. '
            'Do not ask follow-up questions. '
            'If information is missing, make a reasonable assumption and provide the best answer directly. '
            'Avoid long explanations, small talk, and repeated content.'
        )

    @staticmethod
    def _get_optional_float(config, section, option, env_name):
        raw_value = os.getenv(env_name)
        if raw_value is None:
            raw_value = config.get(section, option, fallback="")

        raw_value = str(raw_value).strip()
        if not raw_value:
            return None

        try:
            return float(raw_value)
        except ValueError:
            return None

    @staticmethod
    def _extract_content(response_json):
        choices = response_json.get("choices") or []
        if not choices:
            return ""

        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text_parts.append(str(item.get("text", "")))
                else:
                    text_parts.append(str(item))
            return "".join(text_parts).strip()

        return str(content).strip()

    @staticmethod
    def _extract_usage(response_json):
        usage = response_json.get("usage") or {}
        if not isinstance(usage, dict) or not usage:
            return None

        def _safe_int(*keys):
            for key in keys:
                value = usage.get(key)
                try:
                    if value is not None:
                        return int(value)
                except (TypeError, ValueError):
                    continue
            return 0

        prompt_tokens = _safe_int("prompt_tokens", "input_tokens")
        completion_tokens = _safe_int("completion_tokens", "output_tokens")
        total_tokens = _safe_int("total_tokens")
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        if not any((prompt_tokens, completion_tokens, total_tokens)):
            return None

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _estimate_tokens(text):
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    def _estimate_usage(self, messages, assistant_content):
        prompt_text = "\n".join(str(message.get("content", "")) for message in messages)
        prompt_tokens = self._estimate_tokens(prompt_text)
        completion_tokens = self._estimate_tokens(assistant_content)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def submit_with_metadata(self, user_message: str):
        # Build the conversation history: system + user message
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": user_message},
        ]

        # Prepare the request payload with generation parameters
        payload = {
            "messages": messages,
            "temperature": 1,     # randomness of output (higher = more creative)
            "max_tokens": self.max_tokens,    # maximum length of the reply
            "top_p": 1,         # nucleus sampling parameter
            "stream": False       # disable streaming, wait for full reply
        }

        # Send the request to the ChatGPT REST API
        try:
            response = requests.post(
                self.url,
                json=payload,
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            error_text = str(exc)
            return {
                "ok": False,
                "content": "Error: " + error_text,
                "usage": None,
                "status_code": None,
                "error": error_text,
                "model": self.model,
            }

        try:
            response_json = response.json()
        except ValueError:
            response_json = None

        if response.status_code == 200 and isinstance(response_json, dict):
            content = self._extract_content(response_json) or "No response received."
            usage = self._extract_usage(response_json)
            usage_source = "api"
            if usage is None:
                usage = self._estimate_usage(messages, content)
                usage_source = "estimated"
            return {
                "ok": True,
                "content": content,
                "usage": usage,
                "usage_source": usage_source,
                "status_code": response.status_code,
                "error": None,
                "model": self.model,
            }

        if isinstance(response_json, dict):
            error_body = response_json.get("error", response_json)
            if isinstance(error_body, dict):
                error_text = error_body.get("message") or str(error_body)
            else:
                error_text = str(error_body)
        else:
            error_text = response.text or f"HTTP {response.status_code}"

        return {
            "ok": False,
            "content": "Error: " + error_text,
            "usage": self._extract_usage(response_json)
            if isinstance(response_json, dict)
            else None,
            "usage_source": None,
            "status_code": response.status_code,
            "error": error_text,
            "model": self.model,
        }

    def submit(self, user_message: str):
        return self.submit_with_metadata(user_message)["content"]

