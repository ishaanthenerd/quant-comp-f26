"""
Example: passive market maker with inventory skew. A starting point, not a winning strategy.
Shows: reading the book, quoting inside the limit rules, and using traderData
(here: remembering the last mid price for when one side of the book is empty).
"""
import json
import math

from datamodel import Order

SIZE = 4


class Trader:
    def run(self, state):
        last_mid = json.loads(state.trader_data) if state.trader_data else {}
        orders = {}
        for sym, depth in state.order_depths.items():
            bid, ask = depth.best_bid(), depth.best_ask()
            if bid is not None and ask is not None:
                last_mid[sym] = (bid + ask) / 2
            if sym not in last_mid:
                continue
            fair = last_mid[sym]

            pos, lim = state.position.get(sym, 0), state.position_limits[sym]
            skew = -2.0 * pos / lim                    # long -> quote lower, short -> quote higher
            my_bid = math.floor(fair + skew)
            my_ask = math.ceil(fair + skew)

            # Never ask for more than the limit allows: if buys (or sells) could breach it,
            # ALL of this product's orders are rejected for the tick.
            buy_qty = min(SIZE, lim - pos)
            sell_qty = min(SIZE, lim + pos)
            orders[sym] = []
            if buy_qty > 0:
                orders[sym].append(Order(sym, my_bid, buy_qty))
            if sell_qty > 0:
                orders[sym].append(Order(sym, my_ask, -sell_qty))
        return orders, 0, json.dumps(last_mid)
