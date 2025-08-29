from typing_extensions import NotRequired, TypedDict
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable
from dataclasses import dataclass

class ChatMessage(TypedDict):
    role: str
    content: str
    
class LLMActionStep(TypedDict, total=False):
    action: str
    target: str
    value: str                # the resolved value
    resolve_value: str        # prompt/question to resolve
    orig_value: str           # keep original resolve_value prompt for traceability
    orig_target: str
    locator: Any
    is_expandable_element: bool
    result: str

class LLMResponse(TypedDict, total=False):
    step_execution: str
    step: NotRequired[LLMActionStep | list[LLMActionStep]]
    steps: NotRequired[list[LLMActionStep]]  # if you also support "steps"
    reasoning: str
    answer: str
    
class LocatorActionResult(TypedDict, total=False):
    orig_value: str
    value: str
    orig_target: str
    target: str
    locator: Any
    result: str
        
# --- generic resolver protocol ---
@dataclass(frozen=True)
class ResolveInput:
    text: str
    context: Mapping[str, Any] | None = None

@dataclass(frozen=True)
class ResolveOutput:
    value: Optional[str]

@runtime_checkable
class Resolver(Protocol):
    def resolve(self, inp: ResolveInput) -> "ResolveOutput | str": ...

ResolverLike = Resolver | Callable[[str, Mapping[str, Any] | None], str] | None        