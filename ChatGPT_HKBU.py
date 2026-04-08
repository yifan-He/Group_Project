import requests
import configparser
import os

# A simple client for the ChatGPT REST API
class ChatGPT:
    def __init__(self, config):
        # Read API configuration values from the ini file
        api_key = os.getenv("CHATGPT_KEY") or config.get("CHATGPT", "API_KEY", fallback="")
        base_url = os.getenv("CHATGPT_URL") or config.get("CHATGPT", "BASE_URL", fallback="")
        model = os.getenv("CHATGPT_MODEL") or config.get("CHATGPT", "MODEL", fallback="")
        api_ver = os.getenv("CHATGPT_VER") or config.get("CHATGPT", "API_VER", fallback="")

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

    def submit(self, user_message: str):
        
        # Build the conversation history: system + user message
        messages = [
            {"role": "system", "content": self.system_message},
            {"role": "user", "content": user_message},
        ]

        # Prepare the request payload with generation parameters
        payload = {
            "messages": messages,
            "temperature": 1,     # randomness of output (higher = more creative)
            "max_tokens": 110,    # maximum length of the reply
            "top_p": 1,         # nucleus sampling parameter
            "stream": False       # disable streaming, wait for full reply
        }    

        # Send the request to the ChatGPT REST API
        response = requests.post(self.url, json=payload, headers=self.headers)

        # If successful, return the assistant’s reply text
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            # Otherwise return error details
            return "Error: " + response.text

