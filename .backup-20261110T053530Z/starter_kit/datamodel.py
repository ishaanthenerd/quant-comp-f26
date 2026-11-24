"""
datamodel.py: core data structures for the Quant Club trading competition.

Conventions (kept close to IMC Prosperity so participants feel at home):
  * Prices are integers.
  * Order.quantity > 0 means BUY, < 0 means SELL.
  * OrderDepth.buy_orders maps price -> positive volume (bids).
  * OrderDepth.sell_orders maps price -> NEGATIVE volume (asks).
  * Each call to Trader.run() returns (orders_by_symbol, conversions, trader_data).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

Time = int
Symbol = str
Product = str
Position = int
UserId = str


@dataclass(frozen=True)
class Listing:
    symbol: Symbol
    product: Product
    denomination: str = "SEASHELLS"


@dataclass
class Order:
    symbol: Symbol
    price: int
    quantity: int  # +buy, -sell

    def __post_init__(self):
        if not isinstance(self.price, int) or not isinstance(self.quantity, int):
            raise TypeError("Order price and quantity must be ints")

    def __repr__(self) -> str:
        side = "BUY" if self.quantity > 0 else "SELL"
        return f"Order({self.symbol}, {side} {abs(self.quantity)} @ {self.price})"


@dataclass
class OrderDepth:
    buy_orders: Dict[int, int] = field(default_factory=dict)   # price -> +volume
    sell_orders: Dict[int, int] = field(default_factory=dict)  # price -> -volume

    # Convenience helpers (not in IMC's version, but harmless and handy).
    def best_bid(self) -> Optional[int]:
        return max(self.buy_orders) if self.buy_orders else None

    def best_ask(self) -> Optional[int]:
        return min(self.sell_orders) if self.sell_orders else None

    def mid_price(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2


@dataclass
class Trade:
    symbol: Symbol
    price: int
    quantity: int  # always positive; direction given by buyer/seller
    buyer: Optional[UserId] = None
    seller: Optional[UserId] = None
    timestamp: Time = 0

    def __repr__(self) -> str:
        return (f"Trade({self.symbol}, {self.quantity} @ {self.price}, "
                f"buyer={self.buyer}, seller={self.seller}, t={self.timestamp})")


@dataclass
class Observation:
    """Extra per-tick info not found in the order book (e.g. conversion
    rates, signals, news). Extend this as assets with gimmicks are designed."""
    plain_values: Dict[str, float] = field(default_factory=dict)


@dataclass
class TradingState:
    trader_data: str                                   # string you returned last tick
    timestamp: Time
    listings: Dict[Symbol, Listing]
    order_depths: Dict[Symbol, OrderDepth]
    own_trades: Dict[Symbol, List[Trade]]              # your fills since last tick
    market_trades: Dict[Symbol, List[Trade]]           # bot-vs-bot fills since last tick
    position: Dict[Product, Position]
    position_limits: Dict[Product, int]
    observations: Observation = field(default_factory=Observation)


# Position limits and engine-side constants live in the engine/config,
# not here, so participants cannot rely on editing them.
