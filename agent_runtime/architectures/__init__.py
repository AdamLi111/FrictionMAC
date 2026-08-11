"""
Architecture registry — maps a short name to the Architecture that builds that run option.

Select a variant with the AGENT_ARCH env var (default "v1"), honored by both entry points
(`agent_runtime.main` and `scripts.hw_console`):

    AGENT_ARCH=v2 .venv-agent/bin/python -m agent_runtime.main "go to the mug"

Add a future variant by writing an Architecture subclass and registering it here — nothing in
the run loops changes.
"""
import os

from agent_runtime.architectures.base import Architecture
from agent_runtime.architectures.v1_flat import FlatArchitecture
from agent_runtime.architectures.v2_managers import DomainManagerArchitecture
from agent_runtime.architectures.v3_single import SingleAgentArchitecture
from agent_runtime.architectures.v4_flat_no_approval import FlatNoApprovalArchitecture

_REGISTRY: dict[str, type[Architecture]] = {
    FlatArchitecture.name: FlatArchitecture,
    DomainManagerArchitecture.name: DomainManagerArchitecture,
    SingleAgentArchitecture.name: SingleAgentArchitecture,
    FlatNoApprovalArchitecture.name: FlatNoApprovalArchitecture,
}

DEFAULT_ARCH = "v1"


def available() -> dict[str, str]:
    """{name: description} for every registered architecture (for CLI listing / help)."""
    return {name: cls.description for name, cls in _REGISTRY.items()}


def get(name: str | None = None) -> Architecture:
    """Instantiate an architecture by name; falls back to AGENT_ARCH, then DEFAULT_ARCH."""
    key = (name or os.environ.get("AGENT_ARCH") or DEFAULT_ARCH).strip().lower()
    try:
        return _REGISTRY[key]()
    except KeyError:
        raise SystemExit(
            f"Unknown AGENT_ARCH={key!r}. Available: {', '.join(sorted(_REGISTRY))}"
        )


__all__ = ["Architecture", "available", "get", "DEFAULT_ARCH"]
