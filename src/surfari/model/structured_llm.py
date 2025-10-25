"""
structured_llm.py
-----------------
Unified LLM client for Surfari, supporting proxy and local direct execution.

This version delegates all vendor-specific logic to llm_common.generate_llm_output(),
always passing tools normalized in OpenAI JSON schema format.
"""

import time
import json
import os
import base64
import secrets
import hmac
import hashlib
import requests
from dotenv import load_dotenv
from typing import Dict, Union, List, Optional, Any, Callable, Mapping
from threading import Lock
from jsonfinder import jsonfinder

from surfari.model.llm_common import generate_llm_output, Usage
from surfari.model.tool_helper import _normalize_tools
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger

logger = surfari_logger.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment Management
# ---------------------------------------------------------------------------

_ENV_LOADED = False

def _compute_env_path():
    env_path = os.path.join(config.PROJECT_ROOT, "security", ".env_dev")
    if not os.path.exists(env_path):
        env_path = os.path.join(config.PROJECT_ROOT, "security", ".env")
    return env_path

def _ensure_env_loaded():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = _compute_env_path()
    load_dotenv(dotenv_path=env_path)
    _ENV_LOADED = True


# ---------------------------------------------------------------------------
# Token Stats Helper
# ---------------------------------------------------------------------------

class TokenStats:
    """Thread-safe token usage tracker per purpose (agent name)."""

    def __init__(self):
        self.token_stats = {}
        self.lock = Lock()

    def update_token_stats(self, agent_name: str, prompt_token_count: int,
                           candidates_token_count: int, prompt_token_cached: int = 0):
        """Adds to token counts for a given agent name."""
        with self.lock:
            s = self.token_stats.setdefault(agent_name, {
                "prompt_token_count": 0,
                "prompt_token_cached": 0,
                "candidates_token_count": 0,
                "total_token_count": 0,
            })
            s["prompt_token_count"] += prompt_token_count
            s["prompt_token_cached"] += prompt_token_cached
            s["candidates_token_count"] += candidates_token_count
            s["total_token_count"] += prompt_token_count + candidates_token_count

    def get_token_stats(self) -> Dict[str, Dict[str, int]]:
        with self.lock:
            return self.token_stats.copy()


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

class LLMClient:
    """Unified client for local or proxy LLM invocation."""

    def __init__(self):
        _ensure_env_loaded()
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.token_stats = TokenStats()

    # -----------------------------------------------------------------------
    # Utility Helpers
    # -----------------------------------------------------------------------

    def _parse_llm_response_to_json(self, response_text: str) -> Union[Dict, List, None]:
        """Attempts to parse text output as JSON."""
        if not response_text:
            return None
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            try:
                for _, _, obj in jsonfinder(response_text):
                    if obj is not None:
                        return obj
            except Exception:
                pass
        logger.error(f"Failed to parse JSON from response: {response_text}")
        return None

    # -----------------------------------------------------------------------
    # Main Entry Point
    # -----------------------------------------------------------------------

    async def process_prompt_return_json(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        chat_history: List[Dict[str, Any]] = [],
        image_data: Optional[bytes] = None,
        image_format: str = "jpeg",
        tools: Optional[List[Callable[..., Any]]] = None,
        model: str = "gemini-2.0-flash",
        purpose: str = "navigation",
        site_id: int = 0,
    ) -> Optional[Union[Dict, List]]:
        """
        Unified prompt handler. Returns either:
          - {"tool_calls": [...]} if model emitted structured function calls, or
          - parsed JSON if output is valid JSON text, else None.
        """

        prompt_to_log = system_prompt + json.dumps(chat_history, indent=2) + user_prompt
        await logger.log_text_to_file(site_id, prompt_to_log, purpose, "prompt")

        # ✅ Normalize all tools to OpenAI JSON schema (used both locally and via proxy)
        normalized_tools = _normalize_tools(tools or [])

        # -------------------------------------------------------------------
        # Proxy Mode
        # -------------------------------------------------------------------
        if config.CONFIG["app"].get("use_llm_proxy", True):
            logger.debug("Using Surfari model-router proxy for LLM call.")
            return await self._call_via_proxy(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                chat_history=chat_history,
                image_data=image_data,
                image_format=image_format,
                tools=normalized_tools,
                model=model,
                purpose=purpose,
                site_id=site_id,
            )

        # -------------------------------------------------------------------
        # Local Mode
        # -------------------------------------------------------------------
        logger.debug("Using local llm_common.generate_llm_output().")

        params = dict(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            chat_history=chat_history,
            tools=normalized_tools,
        )

        # Optional image input (convert to base64)
        if image_data:
            params["image_data"] = base64.b64encode(image_data).decode("ascii")
            params["image_format"] = image_format

        result, usage, elapsed_ms = await generate_llm_output(
            params,
            self.openai_api_key,
            self.gemini_api_key,
            self.anthropic_api_key,
        )

        # Track usage stats
        self.token_stats.update_token_stats(
            purpose, usage.prompt, usage.completion, usage.cached
        )

        logger.info(f"Local LLM call ({model}) took {elapsed_ms/1000:.2f}s")

        # Handle tool calls
        if result.get("tool_calls"):
            tool_calls = result["tool_calls"]
            await logger.log_text_to_file(site_id, json.dumps(tool_calls, indent=2),
                                          purpose, "response")
            return {"tool_calls": tool_calls}

        # Otherwise parse JSON text output
        text = result.get("text")
        parsed = self._parse_llm_response_to_json(text) if isinstance(text, str) else text
        await logger.log_text_to_file(site_id, json.dumps(parsed, indent=2) if parsed else "null",
                                      purpose, "response")
        return parsed

    # -----------------------------------------------------------------------
    # Proxy Helper
    # -----------------------------------------------------------------------

    async def _call_via_proxy(
        self,
        system_prompt: str,
        user_prompt: str,
        chat_history: List[Dict[str, Any]],
        image_data: Optional[Union[bytes, str]],
        image_format: str,
        tools: List[Dict[str, Any]],
        model: str,
        purpose: str,
        site_id: int,
        timeout: int = 60,
        return_mode: str = "auto",
    ) -> Union[Dict[str, Any], List[Any], None]:
        """Delegates prompt execution to the Surfari Cloud proxy."""

        _ensure_env_loaded()

        def _ensure_base64(data: Optional[Union[bytes, str]]) -> Optional[str]:
            if not data:
                return None
            if isinstance(data, str):
                return data
            return base64.b64encode(data).decode("ascii")

        proxy_url = os.getenv("SURFARI_PROXY_URL")
        api_key = os.getenv("SURFARI_API_KEY")
        signing_secret = os.getenv("SURFARI_SIGNING_SECRET")

        body_obj = {
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "chat_history": chat_history,
            "image": (
                {"data_base64": _ensure_base64(image_data), "format": image_format}
                if image_data else None
            ),
            "tools": tools or [],
            "purpose": purpose,
            "site_id": site_id,
            "return_mode": return_mode,
        }

        body_json = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        body_bytes = body_json.encode("utf-8")

        # Sign request
        nonce = secrets.token_hex(16)
        ts = str(int(time.time()))
        payload = body_bytes + b"|" + nonce.encode() + b"|" + ts.encode()
        sig = base64.b64encode(
            hmac.new(signing_secret.encode(), payload, hashlib.sha256).digest()
        ).decode()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Surfari-Nonce": nonce,
            "X-Surfari-Timestamp": ts,
            "X-Surfari-Signature": sig,
        }

        start = time.time()
        try:
            resp = requests.post(proxy_url, headers=headers, data=body_bytes, timeout=timeout)
        except Exception as e:
            logger.error(f"Proxy request failed: {e}")
            raise

        elapsed = time.time() - start
        logger.info(f"Proxy call to {model} took {elapsed:.2f}s")

        if resp.status_code != 200:
            logger.error(f"Proxy returned {resp.status_code}: {resp.text}")
            raise RuntimeError(f"Proxy error {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}

        await logger.log_text_to_file(site_id, json.dumps(data, indent=2),
                                      purpose, "proxy_response")

        # Standardize output
        tool_calls = data.get("tool_calls")
        text = data.get("text")

        if tool_calls:
            return {"tool_calls": tool_calls}

        parsed = None
        if isinstance(text, str):
            parsed = self._parse_llm_response_to_json(text)
        elif isinstance(text, (dict, list)):
            parsed = text

        return parsed
