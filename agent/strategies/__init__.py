from .llm_swing import LLMSwingStrategy
from .momentum_macro import MomentumMacroStrategy

STRATEGY_REGISTRY = {
    "momentum_macro": MomentumMacroStrategy,
    "llm_swing": LLMSwingStrategy,
}

__all__ = ["STRATEGY_REGISTRY", "LLMSwingStrategy", "MomentumMacroStrategy"]
