"""
llm_common.py
--------------
Shared logic for vendor-specific LLM calls used by both structured_llm.py (client)
and llm_router.py (Cloud Run server).

Dependencies:
  - openai
  - google-genai
  - anthropic
  - ollama
  - token_meter.Usage
No Surfari imports.
"""

import time, json, base64
from typing import Any, Dict, List, Tuple, Mapping, Optional, Union
from openai import OpenAI
from google import genai
from google.genai import types
from anthropic import Anthropic
import ollama
from dataclasses import dataclass

@dataclass
class Usage:
    vendor: str
    model: str
    prompt: int
    cached: int
    completion: int

    @staticmethod
    def zero(vendor="unknown", model=""):
        return Usage(vendor, model, 0, 0, 0)

    @staticmethod
    def to_wire(u: "Usage"):
        return {
            "vendor": u.vendor, "model": u.model,
            "prompt_tokens": u.prompt,
            "prompt_tokens_cached": u.cached,
            "completion_tokens": u.completion,
            "total_tokens": u.prompt + u.completion
        }

# ----------------------- JSON helpers -----------------------
def _loads_map(s: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(s, str):
        try:
            v = json.loads(s)
            return v if isinstance(v, Mapping) else None
        except Exception:
            return None
    return s if isinstance(s, Mapping) else None


# ----------------------- OpenAI helpers -----------------------
def _get_history_content_for_openai(chat_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert generic history into OpenAI Responses API shape."""
    inputs: List[Dict[str, Any]] = []
    for m in chat_history:
        role, content = m.get("role"), m.get("content")
        if role == "user":
            inputs.append({"role": "user", "content": str(content or "")})
            continue
        if role == "assistant":
            j = _loads_map(content)
            if isinstance(j, Mapping) and isinstance(j.get("tool_calls"), list):
                for c in j["tool_calls"]:
                    name = c.get("name")
                    if not name:
                        continue
                    args = c.get("arguments", {}) or {}
                    if isinstance(args, str):
                        args_map = _loads_map(args)
                        args = args_map if args_map is not None else {"value": args}
                    elif not isinstance(args, Mapping):
                        args = {"value": args}
                    call_id = c.get("call_id") or c.get("id")
                    inputs.append({
                        "type": "function_call",
                        "name": name,
                        "call_id": call_id,
                        "arguments": json.dumps(args),
                    })
            else:
                if content:
                    inputs.append({"role": "assistant", "content": str(content)})
            continue
        if role == "tool":
            payload_map = _loads_map(content)
            payload_str = json.dumps(payload_map if payload_map is not None else {"value": content})
            call_id: Optional[str] = m.get("call_id")
            if call_id:
                inputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": payload_str,
                })
    return inputs


def extract_openai_calls(response) -> List[Dict[str, Any]]:
    """Extract normalized tool_calls from OpenAI Responses API response."""
    try:
        calls = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") == "function_call":
                calls.append({
                    "name": item.name,
                    "arguments": json.loads(item.arguments) if item.arguments else {},
                    "id": getattr(item, "call_id", None),
                })
        return calls
    except Exception:
        return []


# ----------------------- Gemini helpers -----------------------
def _get_history_content_for_gemini(
    chat_history: List[Dict[str, Any]]
) -> List[Union[types.UserContent, types.ModelContent]]:
    """Convert history into Gemini genai types."""
    contents: List[Union[types.UserContent, types.ModelContent]] = []
    for m in chat_history:
        role, content = m.get("role"), m.get("content")
        if role == "assistant":
            parts: List[types.Part] = []
            j = _loads_map(content)
            if isinstance(j, Mapping) and "tool_calls" in j and isinstance(j["tool_calls"], list):
                for c in j["tool_calls"]:
                    args = c.get("arguments", {}) or {}
                    if isinstance(args, str):
                        args = _loads_map(args) or {"value": args}
                    elif not isinstance(args, Mapping):
                        args = {"value": args}
                    parts.append(types.Part.from_function_call(name=c["name"], args=args))
            else:
                if content:
                    parts.append(types.Part.from_text(text=str(content)))
            if parts:
                contents.append(types.ModelContent(parts=parts))
            continue
        if role == "tool":
            payload = _loads_map(content) or {"value": content}
            parts = [types.Part.from_function_response(
                name=m.get("name", "tool"), response=payload)]
            contents.append(types.UserContent(parts=parts))
            continue
        if role == "user":
            contents.append(types.UserContent(parts=[
                types.Part.from_text(text=str(content or ""))
            ]))
            continue
        if role in ("model",) and content:
            contents.append(types.ModelContent(parts=[
                types.Part.from_text(text=str(content))
            ]))
    return contents


def extract_gemini_calls(resp):
    """Extract all Gemini function calls from all candidates."""
    calls = []
    try:
        for cand in getattr(resp, "candidates", []) or []:
            parts = getattr(getattr(cand, "content", None), "parts", []) or []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if fc:
                    calls.append({
                        "name": getattr(fc, "name", None),
                        "arguments": getattr(fc, "args", {}) or {}
                    })
    except Exception:
        pass
    
    return calls


def _make_tools_for_gemini(tools: List[dict]) -> List[types.Tool]:
    out: List[types.Tool] = []
    for t in tools or []:
        params = t.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object"}
        fn_decl = types.FunctionDeclaration(
            name=t.get("name", "unnamed"),
            description=t.get("description", ""),
            parameters=params,
        )
        out.append(types.Tool(function_declarations=[fn_decl]))
    return out


# ----------------------- Core unified executor -----------------------
async def generate_llm_output(p: Dict[str, Any],
                              openai_key: str,
                              gemini_key: str,
                              anthropic_key: str) -> Tuple[Dict[str, Any], Usage, int]:
    """Unified LLM generation across vendors."""
    t0 = time.time()
    model = p["model"]
    system = p.get("system_prompt", "")
    user = p.get("user_prompt", "")
    history = p.get("chat_history", [])
    tools = p.get("tools", []) or []

    image = None
    if "image" in p and isinstance(p["image"], dict):
        image = p["image"]
    elif "image_data" in p and p["image_data"]:
        image = {"data_base64": p["image_data"], "format": p.get("image_format", "jpeg")}

    usage = Usage.zero(vendor="unknown", model=model)
    is_openai = model.startswith(("gpt-", "o3-"))
    is_gemini = model.startswith("gemini-")
    is_anthropic = model.startswith("claude-")
    is_ollama = model.startswith(("deepseek", "qwen", "llama", "gemma"))

    # ---- OpenAI ----
    if is_openai:
        client = OpenAI(api_key=openai_key)
        msgs = [{"role": "system", "content": system}] + _get_history_content_for_openai(history)
        if image:
            data_url = f"data:image/{image.get('format','jpeg')};base64,{image['data_base64']}"
            msgs.append({
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user},
                    {"type": "input_image", "image_url": data_url},
                ],
            })
        else:
            msgs.append({"role": "user", "content": user})
        kwargs = dict(model=model, input=msgs)
        if model.startswith("gpt-5"):
            kwargs["reasoning"] = {"effort": "minimal"}
        if tools:
            kwargs["tools"] = tools
        resp = client.responses.create(**kwargs)
        ocalls = extract_openai_calls(resp)
        prompt_tokens = getattr(resp.usage, "input_tokens", 0)
        cached = getattr(getattr(resp.usage, "input_tokens_details", None), "cached_tokens", 0)
        completion = getattr(resp.usage, "output_tokens", 0)
        usage = Usage(vendor="openai", model=model, prompt=prompt_tokens, cached=cached, completion=completion)
        text = (getattr(resp, "output_text", None) or "").strip()
        return ({"tool_calls": ocalls or None, "text": text}, usage, int((time.time() - t0) * 1000))

    # ---- Gemini ----
    if is_gemini:
        client = genai.Client(api_key=gemini_key)
        contents = _get_history_content_for_gemini(history)
        contents.append(types.UserContent(parts=[types.Part.from_text(text=user)]))
        if image:
            contents.append(
                types.Part.from_bytes(
                    data=base64.b64decode(image["data_base64"]),
                    mime_type=f"image/{image.get('format','jpeg')}",
                )
            )
        cfg = {
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            "system_instruction": system,
        }
        if tools:
            cfg["tools"] = _make_tools_for_gemini(tools)
        resp = client.models.generate_content(
            model=model,
            config=types.GenerateContentConfig(**cfg),
            contents=contents,
        )
        gcalls = extract_gemini_calls(resp)
        usage = Usage(
            vendor="gemini",
            model=model,
            prompt=getattr(resp.usage_metadata, "prompt_token_count", 0),
            cached=0,
            completion=getattr(resp.usage_metadata, "candidates_token_count", 0),
        )
        return ({"tool_calls": gcalls or None, "text": (resp.text or "").strip()},
                usage, int((time.time() - t0) * 1000))

    # ---- Anthropic ----
    if is_anthropic:
        c = Anthropic(api_key=anthropic_key)
        resp = c.messages.create(
            model=model, max_tokens=1024, temperature=0.7,
            system=system, messages=history + [{"role": "user", "content": user}],
        )
        text = resp.content[0].text.strip() if resp.content else ""
        usage = Usage(
            vendor="anthropic", model=model,
            prompt=getattr(resp, "input_tokens", 0),
            cached=0, completion=getattr(resp, "output_tokens", 0),
        )
        return ({"tool_calls": None, "text": text}, usage, int((time.time() - t0) * 1000))

    # ---- Ollama ----
    if is_ollama:
        c = ollama.Client()
        r = c.chat(model=model,
                   messages=[{"role": "system", "content": system}] + history + [{"role": "user", "content": user}])
        text = r["message"].get("content", "")
        usage = Usage(vendor="ollama", model=model, prompt=0, cached=0, completion=0)
        return ({"tool_calls": None, "text": text}, usage, int((time.time() - t0) * 1000))

    raise ValueError(f"Unsupported model: {model}")
