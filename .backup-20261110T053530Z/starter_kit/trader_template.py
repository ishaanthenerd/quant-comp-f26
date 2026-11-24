"""
Your submission is ONE file like this one (max 100 KB) that defines `class Trader`
with a `run(self, state)` method. The server calls run() once per tick.

Return a tuple (orders, conversions, trader_data):
  orders      dict: symbol -> list of Order. Order(symbol, price, quantity), quantity > 0 BUYS, < 0 SELLS.
              price and quantity must be Python ints.
  conversions always 0 for now (reserved).
  trader_data a str (max 50,000 chars). It is handed back to you as state.trader_data on the next tick.

Remember: if your process is restarted (e.g. after a slow tick) ONLY trader_data survives,
so keep anything important in trader_data (json.dumps / json.loads work well).

Allowed imports: datamodel, typing, math, statistics, collections, json, numpy, pandas,
                 itertools, functools, heapq, copy, dataclasses
"""
from datamodel import Order, TradingState


class Trader:
    def run(self, state: TradingState):
        orders = {}

        for symbol, depth in state.order_depths.items():
            position = state.position.get(symbol, 0)
            limit = state.position_limits[symbol]
            best_bid, best_ask = depth.best_bid(), depth.best_ask()   # helpers; None if that side is empty
            # depth.buy_orders  = {price: +volume}   (bids)
            # depth.sell_orders = {price: -volume}   (asks, volumes are NEGATIVE)

            orders[symbol] = []
            # Example: buy 1 at the best ask if flat. Replace with your logic.
            # if best_ask is not None and position < limit:
            #     orders[symbol].append(Order(symbol, best_ask, 1))

        return orders, 0, state.trader_data
