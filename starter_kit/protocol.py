"""
protocol.py: JSON wire format between the TRUSTED engine process and the
UNTRUSTED trader process. JSON only, never pickle: the parent must never
deserialize anything the participant process controls into live objects.
"""
from datamodel import Listing, Observation, Order, OrderDepth, Trade, TradingState

MAX_TRADER_DATA = 50_000
MAX_ORDERS_PER_SYMBOL = 200
MAX_LINE = 1_000_000      # bytes; longer messages are a protocol violation


def _enc_trades(d):
    return {s: [[t.price, t.quantity, t.buyer, t.seller, t.timestamp] for t in ts]
            for s, ts in d.items()}


def _dec_trades(d):
    return {s: [Trade(s, p, q, b, se, t) for p, q, b, se, t in lst] for s, lst in d.items()}


def encode_state(s: TradingState) -> dict:
    return {
        "td": s.trader_data, "ts": s.timestamp,
        "listings": {k: [v.symbol, v.product, v.denomination] for k, v in s.listings.items()},
        "depths": {k: [{str(p): v for p, v in od.buy_orders.items()},
                       {str(p): v for p, v in od.sell_orders.items()}]
                   for k, od in s.order_depths.items()},
        "own": _enc_trades(s.own_trades), "mkt": _enc_trades(s.market_trades),
        "pos": s.position, "lim": s.position_limits,
        "obs": s.observations.plain_values,
    }


def decode_state(d: dict) -> TradingState:
    depths = {}
    for k, (b, a) in d["depths"].items():
        depths[k] = OrderDepth({int(p): v for p, v in b.items()}, {int(p): v for p, v in a.items()})
    return TradingState(
        trader_data=d["td"], timestamp=d["ts"],
        listings={k: Listing(*v) for k, v in d["listings"].items()},
        order_depths=depths,
        own_trades=_dec_trades(d["own"]), market_trades=_dec_trades(d["mkt"]),
        position=d["pos"], position_limits=d["lim"],
        observations=Observation(d["obs"]),
    )


def decode_orders(obj, valid_symbols):
    """Strictly validate an orders payload coming back from the untrusted process.
    Returns {symbol: [Order, ...]} or raises ValueError."""
    if not isinstance(obj, dict):
        raise ValueError("orders payload must be an object")
    out = {}
    for sym, lst in obj.items():
        if sym not in valid_symbols:
            raise ValueError(f"unknown symbol {sym!r}")
        if not isinstance(lst, list) or len(lst) > MAX_ORDERS_PER_SYMBOL:
            raise ValueError("bad order list")
        orders = []
        for item in lst:
            if (not isinstance(item, list) or len(item) != 2
                    or type(item[0]) is not int or type(item[1]) is not int):
                raise ValueError("order must be [int price, int quantity]")
            orders.append(Order(sym, item[0], item[1]))
        out[sym] = orders
    return out
