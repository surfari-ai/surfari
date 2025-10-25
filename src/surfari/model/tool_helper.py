from typing import Any, Dict, List, Sequence, Optional, Union, Tuple, Callable, get_origin, get_args, get_type_hints
from copy import deepcopy
import inspect
import json
from pydantic import BaseModel as PydanticBaseModel, ValidationError as PydanticValidationError
from google.genai import types  

# ---------- Utilities to flatten JSON Schema $defs/$ref for OpenAI ----------

def _resolve_ref(ref: str, defs: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return {}
    key = ref.split("#/$defs/")[-1]
    target = defs.get(key, {})
    return deepcopy(target)

def _flatten_jsonschema(node: Any, defs: Dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            resolved = _resolve_ref(node["$ref"], defs)
            return _flatten_jsonschema(resolved, defs)
        out = {}
        for k, v in node.items():
            if k == "$defs":
                out[k] = v
            else:
                out[k] = _flatten_jsonschema(v, defs)
        return out
    if isinstance(node, list):
        return [_flatten_jsonschema(item, defs) for item in node]
    return node

def _flatten_openai_parameters(parameters: Dict[str, Any]) -> Dict[str, Any]:
    params = deepcopy(parameters)
    defs = params.get("$defs", {})
    flattened = _flatten_jsonschema(params, defs)
    if isinstance(flattened, dict) and "$defs" in flattened:
        flattened.pop("$defs", None)
    return flattened

_PRIMITIVE_MAP: Dict[Any, Dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    dict: {"type": "object"},
    list: {"type": "array"},
}

def _schema_from_annotation(ann: Any) -> Dict[str, Any]:
    """
    Build JSON Schema from a type annotation.
    - Supports Pydantic BaseModel and List[BaseModel].
    - Handles Optional[T]/Union[T, None].
    - Maps primitives.
    """
    origin = get_origin(ann)

    # Optional[T] or Union[..., None]
    if origin is Union:
        args = [a for a in get_args(ann) if a is not type(None)]  # noqa: E721
        if not args:
            return {"type": "null"}
        if len(args) == 1:
            return _schema_from_annotation(args[0])
        return {"anyOf": [_schema_from_annotation(a) for a in args]}

    # List[T] / list[T]
    if origin in (list, List):
        (inner,) = get_args(ann) or (Any,)
        return {"type": "array", "items": _schema_from_annotation(inner)}

    # Dict[K, V] / dict[K, V]
    if origin in (dict, Dict):
        return {"type": "object"}

    # Direct Pydantic model
    if inspect.isclass(ann) and issubclass(ann, PydanticBaseModel):
        # model_json_schema may include $defs/$ref; we flatten later for OpenAI
        return ann.model_json_schema()

    # Primitive / bare types
    if ann in _PRIMITIVE_MAP:
        return _PRIMITIVE_MAP[ann]

    # Unknown — be permissive
    return {"type": ["string", "number", "boolean", "object", "array", "null"]}

def _function_to_spec(fn: Callable) -> Dict[str, Any]:
    """
    Build {name, description, parameters} from a function signature,
    resolving annotations (incl. string/forward refs) via get_type_hints.
    """
    attached = getattr(fn, "__parameters_schema__", None)
    if isinstance(attached, dict):
        params_schema = _flatten_openai_parameters(attached)
        desc = (inspect.getdoc(fn) or "").strip()
        return {
            "name": fn.__name__,
            "description": desc or f"Python tool {fn.__name__}",
            "parameters": params_schema,
        }    
    
    sig = inspect.signature(fn)

    # Resolve annotations even if 'from __future__ import annotations' is used
    try:
        hints = get_type_hints(fn, globalns=getattr(fn, "__globals__", None), localns=None, include_extras=True)
    except Exception:
        hints = {}

    params_schema: Dict[str, Any] = {"type": "object", "properties": {}}
    required: List[str] = []

    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        ann = hints.get(name, p.annotation)  # prefer resolved type
        if ann is inspect._empty:
            ann = Any
        js = _schema_from_annotation(ann)
        params_schema["properties"][name] = js
        if p.default is inspect._empty:
            required.append(name)

    if required:
        params_schema["required"] = required

    desc = (inspect.getdoc(fn) or "").strip()
    return {
        "name": fn.__name__,
        "description": desc or f"Python tool {fn.__name__}",
        "parameters": params_schema,
    }


# -------------------------- OpenAI Normalization -----------------------------
def _normalize_tools(
    tools: Optional[List[Union[Callable, dict]]]
) -> Optional[List[Dict[str, Any]]]:
    """
    Accepts a list of:
      - callables (Python functions)
      - dict specs (either full OpenAI-like tool dicts or bare {"name","description","parameters"})
    Returns OpenAI *Responses API* compatible tool list, i.e. flattened:
      {"type":"function","name":..., "description":..., "parameters":{...}}
    """
    if not tools:
        return None

    out: List[Dict[str, Any]] = []

    for t in tools:
        # 1) Build a spec dict: {"name","description","parameters"}
        if callable(t):
            spec = _function_to_spec(t)
        elif isinstance(t, dict):
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                # Old shape: {"type":"function","function":{...}} -> take inner
                spec = t["function"]
            else:
                # Bare spec
                spec = {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object"}),
                }
            if not spec.get("name"):
                raise ValueError("Tool dict is missing required 'name' field.")
        else:
            raise TypeError(f"Unsupported tool type: {type(t)}")

        # 2) Validate/flatten parameters
        params = spec.get("parameters", {"type": "object"})
        if not isinstance(params, dict):
            raise TypeError("'parameters' must be a dict JSON Schema.")
        params = _flatten_openai_parameters(params)

        # 3) Emit flattened Responses API shape
        out.append({
            "type": "function",
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": params,
        })

    # print(json.dumps(out, indent=2))
    return out or None


def _ensure_list_of_models(items: Sequence[Any], model_cls: type[PydanticBaseModel]) -> Tuple[List[PydanticBaseModel], int]:
    """Coerce a sequence of dicts/models into a list of Pydantic models. Returns (models, invalid_count)."""
    models: List[PydanticBaseModel] = []
    invalid = 0
    for it in items or []:
        try:
            models.append(it if isinstance(it, model_cls) else model_cls.model_validate(it))
        except PydanticValidationError as e:
            print("Failed to validate item: %s", it)
            print("Validation error details:", e.errors())
            invalid += 1
    return models, invalid