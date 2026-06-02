from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ProviderResult:
    """
    Normalised output from any provider.

    status is provider-level only: "success" or "error".
    The caller maps this to an agent-level StatusCode (SUCCESS, BLOCKED, FAILED, etc.).
    """
    text: str
    provider: str
    model: str
    status: str          # "success" | "error"
    error: str | None = None
    usage: dict | None = None


class BaseProvider(ABC):
    @abstractmethod
    def generate(
        self,
        prompt_package: dict,
        model_config: dict,
        on_token: Callable[[str], None] | None = None,
        history: list[dict] | None = None,
    ) -> ProviderResult:
        """
        Make a generation call.

        prompt_package keys:
            system_prompt   str   — agent Markdown instructions
            user_message    str   — task + loaded context content
            agent_name      str
            run_id          str
            task_id         str

        model_config keys:
            provider        str   — "openrouter" | "stub"
            tier            str   — "strong" | "medium" | "cheap"
            model           str   — resolved model ID
            temperature     float

        on_token:
            If provided, called with each text chunk as it arrives (streaming mode).
            If None, returns the full response at once (batch mode).
        """
        ...
