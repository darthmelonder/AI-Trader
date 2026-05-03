from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EntrySignal:
    symbol: str
    quantity: float
    thesis: str
    confidence_factors: list = field(default_factory=list)


@dataclass
class ExitSignal:
    symbol: str
    # "stop_loss" | "signal_flip" | "macro_flip" | "age_limit"
    reason: str
    thesis: str


class Strategy(ABC):
    name: str
    description: str

    @abstractmethod
    def evaluate_entry(
        self,
        symbol: str,
        macro: dict,
        stock: dict,
        news_items: list,
        universe: list,
    ) -> Optional[EntrySignal]:
        """Return EntrySignal if all conditions pass, None otherwise."""

    @abstractmethod
    def evaluate_exit(
        self,
        symbol: str,
        position: dict,
        macro: dict,
        stock: dict,
        current_price: float,
        config: dict,
    ) -> Optional[ExitSignal]:
        """Return ExitSignal if any exit condition triggers, None otherwise."""

    def probe(
        self,
        symbol: str,
        macro: dict,
        stock: dict,
        news_items: list,
    ) -> Optional[dict]:
        """Return full analysis dict for *symbol* bypassing all pre-gates.

        LLM-backed strategies override this to expose raw model reasoning.
        Rule-based strategies may return None (not applicable).
        """
        return None
