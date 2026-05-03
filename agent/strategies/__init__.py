from .llm_swing import LLMSwingStrategy
from .mean_reversion import MeanReversionStrategy
from .momentum_macro import MomentumMacroStrategy

STRATEGY_REGISTRY = {
    "momentum_macro": MomentumMacroStrategy,
    "llm_swing": LLMSwingStrategy,
    "mean_reversion": MeanReversionStrategy,
}

__all__ = ["STRATEGY_REGISTRY", "LLMSwingStrategy", "MeanReversionStrategy", "MomentumMacroStrategy"]
