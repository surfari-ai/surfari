"""
Tool executor for normalized LLM tool calls.

Given a payload like:
    {"tool_calls": [
        {"id": "call_1", "name": "search_web", "arguments": {"q": "pizza"}},
        {"id": "call_2", "name": "open_url",   "arguments": {"url": "https://..."}}
    ]}

this module will locate the corresponding Python callables and invoke them
with keyword arguments, handling both sync and async tools, argument coercion,
timeouts, and JSON-safe result wrapping.

Designed to work with LLMClient.process_prompt_return_json(...),
which already normalizes OpenAI/Gemini tool calls into the above shape.
"""
import asyncio
import inspect
import json
import traceback
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping, Optional
from surfari.util import surfari_logger as _surfari_logger
logger = _surfari_logger.getLogger(__name__)
# ============================
# Public data structures
# ============================

@dataclass
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    id: Optional[str] = None


@dataclass
class ToolResult:
    id: Optional[str]
    name: str
    ok: bool
    result: Any = None
    error: Optional[str] = None

    def json_safe(self) -> Dict[str, Any]:
        """Return a dict that's safe to JSON-serialize."""
        return {
            "id": self.id,
            "name": self.name,
            "ok": self.ok,
            "result": _json_safe(self.result),
            "error": self.error,
        }


# ============================
# Registry helpers
# ============================

def tool_name(fn: Callable[..., Any]) -> str:
    """Resolve the external tool name for a callable.

    Preference order:
    - explicit attribute "tool_name" (settable via a decorator)
    - __name__
    """
    return getattr(fn, "tool_name", None) or getattr(fn, "__name__", str(fn))


def make_registry(tools: Iterable[Callable[..., Any]]) -> Dict[str, Callable[..., Any]]:
    """Build a mapping {tool_name -> callable}. Later tools with the same name win.
    """
    registry: Dict[str, Callable[..., Any]] = {}
    for t in tools:
        name = tool_name(t)
        if not name:
            continue
        registry[name] = t
    logger.debug(f"Registered tools: {list(registry)}")
    return registry


# Optional convenience decorator so you can set names explicitly

def tool(name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, "tool_name", name or fn.__name__)
        return fn
    return _wrap


# ============================
# Core execution API
# ============================

async def execute_tool_calls(
    tool_calls_payload: Mapping[str, Any],
    tools: Iterable[Callable[..., Any]],
    *,
    timeout: Optional[float] = None,
    parallel: bool = False,
    allow_extra_args: bool = True,
    strict_types: bool = False,
) -> Dict[str, Any]:
    """Execute all tool calls and return a normalized result payload.

    Args:
        tool_calls_payload: expects {"tool_calls": [{"name": str, "arguments": (dict|str|list), "id": str?}, ...]}
        tools: iterable of python callables (sync or async). Names resolved via tool_name(fn).
        timeout: optional per-call timeout in seconds.
        parallel: if True, run all calls concurrently; otherwise serial in order.
        allow_extra_args: if False, drop kwargs not in function signature.
        strict_types: if True, do *not* coerce basic JSON-serializable strings to numbers/bools; pass as-is.

    Returns:
        {"tool_results": [ {id, name, ok, result?, error?}, ... ]}
    """
    registry = make_registry(tools)
    raw_calls = list(_extract_calls(tool_calls_payload))

    if parallel and len(raw_calls) > 1:
        tasks = [
            _execute_single(call, registry, timeout=timeout, allow_extra_args=allow_extra_args, strict_types=strict_types)
            for call in raw_calls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    else:
        results = []
        for call in raw_calls:
            res = await _execute_single(
                call, registry, timeout=timeout, allow_extra_args=allow_extra_args, strict_types=strict_types
            )
            results.append(res)

    payload = {"tool_results": [r.json_safe() for r in results]}
    logger.debug(f"Tool results payload: {payload}")
    return payload


# ============================
# Internals
# ============================

def _extract_calls(tool_calls_payload: Mapping[str, Any]) -> Iterable[ToolCall]:
    calls = tool_calls_payload.get("tool_calls") or []
    for c in calls:
        name = c.get("name") if isinstance(c, Mapping) else None
        if not name:
            continue
        args = _normalize_arguments(c.get("arguments"))
        cid = c.get("id")
        yield ToolCall(name=name, arguments=args, id=cid)


def _normalize_arguments(arguments: Any) -> Dict[str, Any]:
    """Turn whatever the model produced into a kwargs dict.

    Accepts:
      - dict (returned as-is)
      - str (attempt json.loads, else leave as single value under "value")
      - list (either list of {name, value} objects or [ [k, v], ... ])
      - None (empty dict)
    """
    if arguments is None:
        return {}
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            loaded = json.loads(arguments)
            return loaded if isinstance(loaded, Mapping) else {"value": loaded}
        except Exception:
            return {"value": arguments}
    if isinstance(arguments, list):
        # Try list of {name, value}
        if all(isinstance(x, Mapping) and "name" in x and "value" in x for x in arguments):
            return {x["name"]: x["value"] for x in arguments}
        # Try list of pairs
        if all(isinstance(x, (list, tuple)) and len(x) == 2 for x in arguments):
            return {k: v for k, v in arguments}
        return {"items": arguments}
    # Fallback: single value under generic key
    return {"value": arguments}


async def _execute_single(
    call: ToolCall,
    registry: Mapping[str, Callable[..., Any]],
    *,
    timeout: Optional[float],
    allow_extra_args: bool,
    strict_types: bool,
) -> ToolResult:
    name = call.name
    func = registry.get(name)
    if not func:
        msg = f"Unknown tool: {name}"
        logger.warning(msg)
        return ToolResult(id=call.id, name=name, ok=False, error=msg)

    try:
        kwargs = _filter_kwargs_for(func, dict(call.arguments), allow_extra=allow_extra_args, strict_types=strict_types)
    except Exception as e:
        return ToolResult(id=call.id, name=name, ok=False, error=f"Argument error: {e}")

    async def _run() -> Any:
        try:
            if inspect.iscoroutinefunction(func):
                return await func(**kwargs)
            # Run sync function in a thread to allow cancellation via timeout
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: func(**kwargs))
        except Exception as e:
            logger.debug("\n" + traceback.format_exc())
            raise

    try:
        if timeout and timeout > 0:
            result = await asyncio.wait_for(_run(), timeout=timeout)
        else:
            result = await _run()
        return ToolResult(id=call.id, name=name, ok=True, result=result)
    except asyncio.TimeoutError:
        return ToolResult(id=call.id, name=name, ok=False, error=f"Timeout after {timeout}s")
    except Exception as e:
        return ToolResult(id=call.id, name=name, ok=False, error=f"{type(e).__name__}: {e}")


def _filter_kwargs_for(
    func: Callable[..., Any],
    kwargs: MutableMapping[str, Any],
    *,
    allow_extra: bool,
    strict_types: bool,
) -> Dict[str, Any]:
    """Bind/trim kwargs according to the function signature.

    - If allow_extra=False, drop keys not in the signature (unless **kwargs is present).
    - If strict_types=True, do no coercion; otherwise apply minor safe coercions.
    """
    sig = inspect.signature(func)

    # If function accepts **kwargs, we can pass anything
    accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    if not allow_extra and not accepts_var_kwargs:
        permitted = {k: kwargs[k] for k in list(kwargs.keys()) if k in sig.parameters}
    else:
        permitted = dict(kwargs)

    if not strict_types:
        for k, v in list(permitted.items()):
            permitted[k] = _coerce_json_scalar(v)

    # Bind to catch missing required params early
    try:
        sig.bind_partial(**permitted)  # will raise if required params are missing when not provided at all
    except TypeError as e:
        # Provide clearer error message
        raise TypeError(str(e))

    return permitted


def _coerce_json_scalar(v: Any) -> Any:
    """Lightweight, safe-ish coercions for common cases (strings -> ints/floats/bools).
    Only applies to scalars and simple lists/dicts recursively.
    """
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        # int
        if s and (s.isdigit() or (s[0] in "+-" and s[1:].isdigit())):
            try:
                return int(s)
            except Exception:
                pass
        # float
        try:
            if any(ch in s for ch in ".eE"):
                return float(s)
        except Exception:
            pass
        return v
    if isinstance(v, list):
        return [_coerce_json_scalar(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce_json_scalar(x) for k, x in v.items()}
    return v


def _json_safe(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except Exception:
        try:
            return json.loads(json.dumps(obj, default=_fallback_serialize))
        except Exception:
            return repr(obj)


def _fallback_serialize(o: Any) -> Any:
    if dataclass_isinstance(o):
        return asdict(o)
    if isinstance(o, Exception):
        return {"error": type(o).__name__, "message": str(o)}
    if hasattr(o, "__dict__"):
        return dict(o.__dict__)
    return repr(o)


def dataclass_isinstance(obj: Any) -> bool:
    try:
        from dataclasses import is_dataclass
        return is_dataclass(obj) and not isinstance(obj, type)
    except Exception:
        return False


# ============================
# Example usage (kept minimal)
# ============================

async def _example():  # pragma: no cover - illustrative only
    # Define some tools
    @tool("add")
    def add(a: int, b: int) -> int:
        return a + b

    @tool("sleep_then")
    async def sleep_then(seconds: float, value: str) -> str:
        await asyncio.sleep(seconds)
        return value

    payload = {
        "tool_calls": [
            {"id": "t1", "name": "add", "arguments": {"a": 2, "b": "40"}},
            {"id": "t2", "name": "sleep_then", "arguments": {"seconds": 0.01, "value": "done"}},
        ]
    }

    results = await execute_tool_calls(payload, tools=[add, sleep_then], parallel=False)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_example())
