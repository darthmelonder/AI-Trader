from .momentum_macro import MomentumMacroStrategy

STRATEGY_REGISTRY = {
    "momentum_macro": MomentumMacroStrategy,
}

__all__ = ["STRATEGY_REGISTRY", "MomentumMacroStrategy"]
