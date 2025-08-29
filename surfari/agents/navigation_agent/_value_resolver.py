from typing import Any, Mapping, Optional
from copy import deepcopy
import importlib
import inspect
import tldextract
from surfari.agents.navigation_agent._typing import (
    LLMActionStep,
    LLMResponse,
    ResolveInput,
    ResolveOutput,
    Resolver,
    ResolverLike
)
import surfari.util.surfari_logger as surfari_logger
from surfari.security.site_credential_manager import SiteCredentialManager

logger = surfari_logger.getLogger(__name__)

def extract_steps(resp: LLMResponse) -> Optional[list[LLMActionStep]]:
    """
    Return a *list of step dicts* from `resp` or None if neither `step` nor `steps`
    is present in a valid form.
    - `step`: dict → [dict], list[dict] → list
    - `steps`: list[dict] → list
    """
    if "step" in resp and resp["step"] is not None:
        s = resp["step"]
        if isinstance(s, dict):
            return [s]  # single step
        if isinstance(s, list):
            return s    # already a list of steps
        return None

    if "steps" in resp and resp["steps"] is not None:
        s = resp["steps"]
        if isinstance(s, list):
            return s
        return None

    return None

def _response_has_resolve_value(resp: LLMResponse) -> bool:
    """
    True if `resp` has a `step` or `steps` branch containing at least one
    step dict with a non-empty string `resolve_value`.
    """
    steps = extract_steps(resp)
    if not steps:
        return False

    for st in steps:
        rv = st.get("resolve_value")
        if isinstance(rv, str) and rv.strip():
            return True
    return False

def _call_resolver(
    resolver: ResolverLike,
    prompt: str,
    context: Optional[Mapping[str, Any]]
) -> Optional[str]:
    if resolver is None:
        return None
    if isinstance(resolver, Resolver):
        out = resolver.resolve(ResolveInput(text=prompt, context=context))
        return out.value if isinstance(out, ResolveOutput) else out
    return resolver(prompt, context)  # type: ignore[misc]

def _resolve_steps(steps: list[LLMActionStep], resolver: ResolverLike, context: Optional[Mapping[str, Any]]) -> None:
    assert resolver is not None, "_resolve_steps called with resolver=None"    
    for step in steps:
        if "value" in step:
            step.pop("resolve_value", None)
            continue
        rv = step.get("resolve_value")
        if isinstance(rv, str) and rv.strip():
            prompt = rv.strip()
            try:
                resolved = _call_resolver(resolver, prompt, context)
            except Exception as e:
                logger.exception("Resolver failed for prompt %r (step target=%r)", prompt, step.get("target"))
                continue
            if resolved:
                step["orig_value"] = prompt
                step["value"] = resolved
                # Always remove resolve_value once processed or skipped
                del step["resolve_value"]

def resolve_missing_value_in_llm_response(
    resp: LLMResponse,
    resolver: ResolverLike,
    *,
    context: Optional[Mapping[str, Any]] = None,
    mutate: bool = False
) -> LLMResponse:
    """
    Resolves `resolve_value` → `value` for whichever branch exists:
      - `step`: dict or list[dict]
      - `steps`: list[dict]

    For each step that has `resolve_value` (and no `value` already):
      - sets `orig_value` to the original prompt
      - sets `value` to the resolved string
      - deletes `resolve_value`
    """
    steps_to_process = extract_steps(resp)
    if not steps_to_process:
        return resp  # nothing to do
    
    for step in steps_to_process:
        if "resolve_value" in step and (step["resolve_value"] == "OTP" or "**" in step["resolve_value"]):
            step["orig_value"] = step["resolve_value"].strip()
            step["value"] = step["resolve_value"].strip()
            del step["resolve_value"]

    has_rv = _response_has_resolve_value(resp)
    if not has_rv:
        return resp  # nothing to do

    out: LLMResponse = resp if mutate else deepcopy(resp)  # type: ignore[assignment]
    steps_to_process = extract_steps(out)

    secret_resolver = SecretResolver((context or {}).get("site_id", 9999))
    _resolve_steps(steps_to_process, secret_resolver, context)
    
    has_rv = _response_has_resolve_value(out)
    if not has_rv:
        return out  # Done resolving secrets

    if resolver:
        _resolve_steps(steps_to_process, resolver, context)
    
    has_rv = _response_has_resolve_value(out)
    if not has_rv:
        return out  # Done resolving values       

    logger.warning("Some values could not be resolved, delegating to user.")
    return DefaultDelegationResolver().delegate_to_user(out, mutate=mutate)

# code to load a custom value resolver
class ResolverLoadError(RuntimeError):
    pass

def _import_obj(target: str) -> Any:
    mod_path, sep, qual = target.partition(":")
    if not sep or not qual:
        raise ResolverLoadError(f"Resolver target must look like 'pkg.mod:Name', got '{target}'")
    try:
        module = importlib.import_module(mod_path)
    except Exception as e:
        raise ResolverLoadError(f"Failed to import module '{mod_path}': {e}") from e
    obj = module
    for part in qual.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as e:
            raise ResolverLoadError(f"'{qual}' not found in '{mod_path}': missing '{part}'") from e
    return obj

def _validate_callable_two_args(fn: Any) -> bool:
    if not callable(fn):
        return False
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    # We require at least two params (prompt, context). They can be positional-or-keyword.
    return len(params) >= 2

def _as_resolver_like(obj: Any) -> ResolverLike:
    if isinstance(obj, Resolver):
        return obj
    if _validate_callable_two_args(obj):
        return obj  # type: ignore[return-value]
    raise ResolverLoadError(
        "Loaded resolver is neither a Resolver instance nor a callable(prompt, context) -> str."
    )

def _normalize_resolver_cfg(cfg: str | Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """
    Returns (target, params) from either a string or a mapping config.
    """
    if isinstance(cfg, str):
        return cfg.strip(), {}
    if not isinstance(cfg, Mapping):
        raise ResolverLoadError("Resolver config must be a string or a mapping.")
    target = cfg.get("target")
    if not isinstance(target, str):
        raise ResolverLoadError("Resolver mapping must include a string 'target' key.")
    params = cfg.get("params") or {}
    if not isinstance(params, Mapping):
        raise ResolverLoadError("'params' must be a mapping if provided.")
    return target.strip(), params


def _build_resolver_from_target(target: str, params: Mapping[str, Any]) -> ResolverLike:
    """
    Handles alias, class, factory, or callable.
    - If class: instantiate with **params.
    - If callable: try factory(**params) first; if that fails and params is empty,
      try zero-arg factory(); otherwise treat it as the final callable.
    """
    logger.info(f"Building resolver from target: {target}, params: {params}")

    obj = _import_obj(target)

    # Class -> instance
    if inspect.isclass(obj):
        try:
            instance = obj(**params)
        except TypeError as e:
            raise ResolverLoadError(f"Failed to construct resolver class: {e}") from e
        return _as_resolver_like(instance)

    # Callable -> factory or final callable
    if callable(obj):
        # Prefer factory(**params)
        try:
            produced = obj(**params)
            if produced is not None:
                return _as_resolver_like(produced)
        except TypeError:
            # If no params provided, try zero-arg factory()
            if not params:
                try:
                    produced = obj()
                    if produced is not None:
                        return _as_resolver_like(produced)
                except TypeError:
                    pass
        # Otherwise, treat the callable itself as the resolver
        return _as_resolver_like(obj)

    raise ResolverLoadError(f"Unsupported resolver object: {type(obj)!r}")


def create_resolver_from_config(cfg: str | Mapping[str, Any]) -> ResolverLike:
    """
    cfg can be:
      - "pkg.mod:ClassName"
      - "pkg.mod:function_name" (callable(prompt, context) -> str)
      - {"target": "...", "params": {...}}  # for class __init__ or factory(**params)
    """
    target, params = _normalize_resolver_cfg(cfg)
    return _build_resolver_from_target(target, params)

def base_domains_match(url1: str, url2: str) -> bool:
    """
    Returns True if the base registrable domains match for the two URLs.
    For example:
        login.sbc.com  -> sbc.com
        www.sbc.com    -> sbc.com
        sub.level.sbc.com/hello?jsp -> sbc.com
    """
    def get_base_domain(url: str) -> str:
        # Ensure scheme so urlparse works
        if "://" not in url:
            url = "http://" + url
        extracted = tldextract.extract(url)
        return f"{extracted.domain}.{extracted.suffix}".lower()

    return get_base_domain(url1) == get_base_domain(url2)

class SecretResolver:
    """
    A resolver that always returns a secret value.
    Conforms to `Resolver` but you shouldn't call `.resolve()` on it.
    """
    def __init__(self, site_id: int | str | None):
        self.site_id = site_id
        self.site_secrets = SiteCredentialManager().load_site_with_secrets(site_id)

    def resolve(self, inp: ResolveInput) -> ResolveOutput:
        logger.debug(f"SecretResolver called with input: {inp.text} and context: {inp.context}")
        current_url = inp.context.get("current_url", None) if inp.context else None
        site_url = self.site_secrets.get("URL")
        logger.debug(f"Matching base domain and suffix of current URL {current_url} with site URL {site_url}")
                
        if current_url and site_url and base_domains_match(current_url, site_url):
            secret_value = self.site_secrets.get(inp.text)
            if secret_value:
                return ResolveOutput(value=secret_value)
        return ResolveOutput(value=None)

class DefaultDelegationResolver:
    """
    A resolver that always delegates to the user.
    Conforms to `Resolver` but you shouldn't call `.resolve()` on it.
    """
    def resolve(self, inp: ResolveInput) -> ResolveOutput:  # type: ignore[override]
        raise RuntimeError("DefaultDelegationResolver.resolve() should not be called.")
    
    def delegate_to_user(
        self,
        resp: LLMResponse,
        *,
        mutate: bool = False,
    ) -> LLMResponse:
        out = resp if mutate else deepcopy(resp)
        out.pop("step", None)
        out.pop("steps", None)
        out["step_execution"] = "DELEGATE_TO_USER"
        out["reasoning"] = "Delegated to user for input: " + out.get("reasoning", "No reasoning provided.")
        return out

# ---------- Minimal examples (optional) ----------
class EchoResolver:
    def resolve(self, inp: ResolveInput) -> ResolveOutput:
        logger.debug(f"EchoResolver called with input: {inp.text} and context: {inp.context}")
        return ResolveOutput(value=inp.text)

class NoOpResolver:
    def resolve(self, inp: ResolveInput) -> ResolveOutput:
        logger.debug(f"NoOpResolver called with input: {inp.text} and context: {inp.context}")
        return ResolveOutput(value=None)

def callable_with_context(prompt: str, ctx: Optional[Mapping[str, Any]] = None) -> str:
    logger.debug(f"callable_with_context called with prompt: {prompt}, context: {ctx}")
    if any(term in prompt.strip().lower() for term in ["source", "origin", "departure"]):
        return "New York"
    elif any(term in prompt.strip().lower() for term in ["destination", "target"]):
        return "Miami"
    return (ctx or {}).get(prompt, "Not Matched")
