import time
import json
import os
from dotenv import load_dotenv
from typing import Dict, Union, List, Optional, Any, Callable, Mapping
from threading import Lock
from jsonfinder import jsonfinder
import ollama
from openai import OpenAI
from google import genai
from google.genai import types
from anthropic import Anthropic
import base64
import re

from surfari.model.tool_helper import (
    _normalize_tools_for_openai,
    _normalize_tools_for_gemini,
    _extract_openai_tool_calls,
    _extract_gemini_tool_calls,
)
import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

# Load environment variables once at the module level.
# This ensures that all instances of LLMClient can access them.
env_path = os.path.join(config.PROJECT_ROOT, "security", ".env_dev")
if not os.path.exists(env_path):
    env_path = os.path.join(config.PROJECT_ROOT, "security", ".env")

logger.debug(f"Loading environment variables from {env_path}")
load_dotenv(dotenv_path=env_path)


class TokenStats:
    """
    A class to manage token statistics, ensuring thread-safe updates.
    Each LLMClient instance will have its own TokenStats object.
    """
    def __init__(self):
        self.token_stats = {}
        self.lock = Lock()

    def update_token_stats(self, agent_name, prompt_token_count, candidates_token_count, prompt_token_cached=0):
        """Adds to the token counts for a given agent name."""
        with self.lock:
            if agent_name not in self.token_stats:
                self.token_stats[agent_name] = {
                    "prompt_token_count": 0,
                    "prompt_token_cached": 0,
                    "candidates_token_count": 0,
                    "total_token_count": 0,
                }
            self.token_stats[agent_name]["prompt_token_count"] += prompt_token_count
            self.token_stats[agent_name]["prompt_token_cached"] += prompt_token_cached
            self.token_stats[agent_name]["candidates_token_count"] += candidates_token_count
            self.token_stats[agent_name]["total_token_count"] += prompt_token_count + candidates_token_count

    def get_token_stats(self) -> Dict[str, Dict[str, int]]:
        """Returns a copy of the current token statistics."""
        with self.lock:
            return self.token_stats.copy()

class LLMClient:
    """
    A client class to handle all LLM interactions, including token tracking.
    Each instance maintains its own TokenStats object.
    """
    def __init__(self):
        """
        Initializes the LLMClient with API keys and its own TokenStats instance.
        """
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.token_stats = TokenStats()

    def _parse_llm_response_to_json(self, response_text: str) -> Union[Dict, List, None]:
        """
        Parses a response string to a JSON object, handling potential formatting issues.
        """
        json_obj = None
        try:
            json_obj = json.loads(response_text)
        except json.JSONDecodeError:
            try:
                results = jsonfinder(response_text)
                for _, _, found_json_obj in results:
                    if found_json_obj is not None:
                        json_obj = found_json_obj
                        break
            except Exception:
                pass
        if not json_obj:
            logger.error(f"Failed to parse JSON from response: {response_text}")
        return json_obj

    def _loads_map(self, s: Any) -> Optional[Mapping[str, Any]]:
        if isinstance(s, str):
            try:
                v = json.loads(s)
                return v if isinstance(v, Mapping) else None
            except Exception:
                return None
        return s if isinstance(s, Mapping) else None

    def _get_history_content_for_gemini(
        self,
        chat_history: List[Dict[str, Any]]
    ) -> List[Union[types.UserContent, types.ModelContent]]:
        contents: List[Union[types.UserContent, types.ModelContent]] = []
        for m in chat_history:
            role = m.get("role")
            content = m.get("content")

            # Assistant: check if content JSON embeds tool_calls
            if role == "assistant":
                parts: List[types.Part] = []
                j = self._loads_map(content)

                # If content has tool_calls, emit functionCall parts
                if isinstance(j, Mapping) and "tool_calls" in j and isinstance(j["tool_calls"], list):
                    for c in j["tool_calls"]:
                        args = c.get("arguments", {}) or {}
                        if isinstance(args, str):
                            args = self._loads_map(args) or {"value": args}
                        elif not isinstance(args, Mapping):
                            args = {"value": args}
                        parts.append(types.Part.from_function_call(
                            name=c["name"],
                            args=args
                        ))
                else:
                    # otherwise, treat as normal text if present
                    if content:
                        parts.append(types.Part.from_text(text=str(content)))

                if parts:
                    contents.append(types.ModelContent(parts=parts))
                continue

            # Tool results → functionResponse parts (Gemini matches by order)
            if role == "tool":
                payload = self._loads_map(content)
                if payload is None:
                    payload = {"value": content}
                parts = [types.Part.from_function_response(
                    name=m.get("name", "tool"),
                    response=payload
                )]
                contents.append(types.UserContent(parts=parts))
                continue

            # Regular user text
            if role == "user":
                contents.append(types.UserContent(parts=[
                    types.Part.from_text(text=str(content or ""))
                ]))
                continue

            # Fallback assistant/model text
            if role in ("assistant", "model") and content:
                contents.append(types.ModelContent(parts=[
                    types.Part.from_text(text=str(content))
                ]))
        # logger.sensitive("Rebuilt Gemini contents: %s", contents)
        return contents


    def _get_history_content_for_openai(self, chat_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert your internal history into an OpenAI Responses API 'input' list.

        Rules:
        - role=='user'  -> {"role":"user","content": "..."}
        - role=='assistant' with content JSON containing {"tool_calls":[...]}
                -> one {"type":"function_call", name, call_id, arguments} per call
            else regular assistant text -> {"role":"assistant","content": "..."}
        - role=='tool'  -> {"type":"function_call_output","call_id": <match>, "output": "<json str>"}
            (matched to the most recent unmatched function_call, by order; fallback by name)

        Returns:
        A list of items ready for OpenAI Responses API.
        """
        inputs: List[Dict[str, Any]] = []

        for m in chat_history:
            role = m.get("role")
            content = m.get("content")

            # --- USER TEXT ---
            if role == "user":
                inputs.append({"role": "user", "content": str(content or "")})
                continue

            # --- ASSISTANT: tool_calls encoded in content JSON ---
            if role == "assistant":
                j = self._loads_map(content)
                if isinstance(j, Mapping) and isinstance(j.get("tool_calls"), list):
                    for c in j["tool_calls"]:
                        name = c.get("name")
                        if not name:
                            # Skip malformed call
                            continue
                        args = c.get("arguments", {}) or {}
                        if isinstance(args, str):
                            args_map = self._loads_map(args)
                            args = args_map if args_map is not None else {"value": args}
                        elif not isinstance(args, Mapping):
                            args = {"value": args}
                        call_id = c.get("call_id") or c.get("id")
                        # Emit a function_call item
                        inputs.append({
                            "type": "function_call",
                            "name": name,
                            "call_id": call_id,
                            "arguments": json.dumps(args),  # must be a JSON string
                        })
                else:
                    # Regular assistant text (if any)
                    if content:
                        inputs.append({"role": "assistant", "content": str(content)})
                continue

            # --- TOOL RESULTS -> function_call_output ---
            if role == "tool":
                # Determine which pending call this output answers
                payload_map = self._loads_map(content)
                payload_str = json.dumps(payload_map if payload_map is not None else {"value": content})

                call_id: Optional[str] = m.get("call_id")
                if call_id:
                    inputs.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": payload_str,
                    })
                # If no call_id available, we skip; alternatively, raise/log here.
                continue
        logger.sensitive("Rebuilt OpenAI inputs: %s", json.dumps(inputs, indent=2))
        return inputs


    async def process_prompt_return_json(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        chat_history: List[Dict[str, str]] = [],
        image_data: Optional[bytes] = None,
        image_format: str = "jpeg",
        tools: List[Callable[..., Any]] = [],
        model: str = "gemini-2.0-flash",
        purpose: str = "navigation",
        site_id: int = 0
    ) -> Union[Dict, List, None]:
        """
        Sends a prompt and returns either:
          - Parsed JSON from text (existing behavior), OR
          - {"tool_calls": [{"name":..., "arguments": {...}, "id": ...?}, ...]}
            when the model decides to call tools (OpenAI or Gemini).
        """
     
        prompt_to_log = system_prompt + json.dumps(chat_history, indent=2) + user_prompt
        await logger.log_text_to_file(site_id, prompt_to_log, purpose, "prompt")

        start = time.time()
        is_open_ai = model.startswith("gpt-") or model == "o3-mini"
        is_gemini_ai = model.startswith("gemini-")
        is_anthropic = model.startswith("claude-")
        is_ollama = model.startswith("deepseek") or model.startswith("qwen") or model.startswith("llama") or model.startswith("gemma")

        messages = []
        messages.append({"role": "system", "content": system_prompt})
        if is_ollama:
            messages += chat_history
        elif is_open_ai:
            messages += self._get_history_content_for_openai(chat_history)
            
        if image_data and is_open_ai:
            b64 = base64.b64encode(image_data).decode("ascii")
            data_url = f"data:image/{image_format};base64,{b64}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            })
        else:
            messages.append({"role": "user", "content": user_prompt})

        responsetxt = ""
        tool_calls_payload: Optional[Dict[str, Any]] = None

        if is_open_ai:
            client = OpenAI(api_key=self.openai_api_key)
            if model.startswith("gpt-5"):
                kwargs = dict(model=model, input=messages, reasoning={"effort": "minimal"})
            else:
                kwargs = dict(model=model, input=messages)
                
            if tools:
                kwargs["tools"] = _normalize_tools_for_openai(tools)
            response = client.responses.create(**kwargs)
            logger.sensitive("OpenAI response: %s", response)
            # First, try to extract tool calls
            ocalls = _extract_openai_tool_calls(response)
            if ocalls:
                tool_calls_payload = {"tool_calls": ocalls}
            else:
                # Fallback to plain text
                responsetxt = (getattr(response, "output_text", None) or "").strip()

            usage = getattr(response, "usage", None)
            if usage:
                logger.debug(f"OpenAI response usage: {usage}")
                input_tokens = getattr(usage, "input_tokens", 0)
                cached_tokens = 0
                if hasattr(usage, "input_tokens_details") and usage.input_tokens_details:
                    cached_tokens = getattr(usage.input_tokens_details, "cached_tokens", 0)
                output_tokens = getattr(usage, "output_tokens", 0)
                self.token_stats.update_token_stats(purpose, input_tokens, output_tokens, cached_tokens)

        elif is_gemini_ai:
            client = genai.Client(api_key=self.gemini_api_key)
            userContent = types.UserContent(parts=[types.Part.from_text(text=user_prompt)])
            config_args = {
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
                "system_instruction": system_prompt,
            }
            if tools:
                config_args["tools"] = _normalize_tools_for_gemini(tools)
            if model.startswith("gemini-2.5"):
                config_args["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

            contents = self._get_history_content_for_gemini(chat_history) + [userContent]
            if image_data:
                contents = contents + [types.Part.from_bytes(data=image_data, mime_type=f"image/{image_format}")]

            response = client.models.generate_content(
                model=model,
                config=types.GenerateContentConfig(**config_args),
                contents=contents
            )

            logger.sensitive("Gemini response: %s", response)
            # Prefer tool calls if present
            gcalls = _extract_gemini_tool_calls(response)
            if gcalls:
                tool_calls_payload = {"tool_calls": gcalls}
            else:
                responsetxt = (response.text or "").strip()

            usage = response.usage_metadata
            if usage:
                logger.debug(f"Gemini response usage: {usage}")
                
                self.token_stats.update_token_stats(purpose, response.usage_metadata.prompt_token_count, response.usage_metadata.candidates_token_count)

        elif is_anthropic:
            client = Anthropic(api_key=self.anthropic_api_key)
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=0.7,
                system=system_prompt,
                messages=chat_history + [{"role": "user", "content": user_prompt}],
            )
            logger.sensitive("Anthropic response: %s", response)
            responsetxt = response.content[0].text.strip() if response.content else ""
            usage = getattr(response, "usage", None)
            if usage:
                logger.debug(f"Anthropic response usage: {usage}")
                self.token_stats.update_token_stats(purpose, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))

        elif is_ollama:
            client = ollama.Client()
            response = client.chat(model=model, messages=messages)
            logger.sensitive("Ollama response: %s", response)
            responsetxt = response["message"].get("content")

        else:
            raise ValueError("Invalid model")

        end = time.time()
        logger.info(f"Time taken to call LLM : {end - start:.2f}s")

        # If tools were invoked, return them (caller will execute)
        if tool_calls_payload:
            logger.sensitive(f"LLM tool calls: {tool_calls_payload}")
            await logger.log_text_to_file(site_id, json.dumps(tool_calls_payload, indent=2), purpose, "response")
            return tool_calls_payload

        # Else parse JSON from text
        responsejson = self._parse_llm_response_to_json(responsetxt) if responsetxt else None
        await logger.log_text_to_file(site_id, json.dumps(responsejson, indent=2) if responsejson is not None else "null", purpose, "response")
        return responsejson
