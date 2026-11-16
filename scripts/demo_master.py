"""Drive the master demo terminal for copy-mode testing.

Attaches to the IC Markets demo terminal over the MetaTrader5 API and places/changes
the master trades the copier then mirrors. Hard-wired to one demo login: it refuses
to touch anything else.

    uv run python scripts/demo_master.py open BTCUSD buy 0.1 450 450
    uv run python scripts/demo_master.py modify <ticket> <sl> <tp>
    uv run python scripts/demo_master.py partial <ticket> <fraction>
    uv run python scripts/demo_master.py close <ticket>
    uv run python scripts/demo_master.py positions
"""

from __future__ import annotations

import json
import sys

import MetaTrader5 as mt5

PATH = r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe"
EXPECT_LOGIN = 52978102  # 52978102@ICMarketsSC-Demo, the noted test master
MAGIC = 777001


def boot() -> None:
    if not mt5.initialize(path=PATH):
        sys.exit(f"init failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None or acct.login != EXPECT_LOGIN:
        mt5.shutdown()
        sys.exit(f"wrong account ({acct and acct.login}); refusing")
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        sys.exit("NOT A DEMO ACCOUNT; refusing")


def _say(result) -> None:
    print(
        json.dumps(
            {
                "retcode": result.retcode,
                "comment": result.comment,
                "order": getattr(result, "order", None),
                "deal": getattr(result, "deal", None),
            }
        )
    )


def cmd_open(symbol: str, side: str, lots: float, sl_off: float, tp_off: float) -> None:
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if side == "buy":
        price, sl, tp, typ = tick.ask, tick.ask - sl_off, tick.ask + tp_off, mt5.ORDER_TYPE_BUY
    else:
        price, sl, tp, typ = tick.bid, tick.bid + sl_off, tick.bid - tp_off, mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": typ,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 50,
        "magic": MAGIC,
        "comment": "copier-test",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    _say(mt5.order_send(req))


def _pos(ticket: int):
    poss = mt5.positions_get(ticket=ticket)
    if not poss:
        sys.exit(f"no position {ticket}")
    return poss[0]


def cmd_modify(ticket: int, sl: float, tp: float) -> None:
    p = _pos(ticket)
    _say(
        mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": p.symbol,
                "sl": sl,
                "tp": tp,
            }
        )
    )


def _close_volume(ticket: int, volume: float) -> None:
    p = _pos(ticket)
    tick = mt5.symbol_info_tick(p.symbol)
    buy = p.type == mt5.POSITION_TYPE_BUY
    _say(
        mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": ticket,
                "symbol": p.symbol,
                "volume": volume,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if buy else tick.ask,
                "deviation": 50,
                "magic": MAGIC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
        )
    )


def cmd_partial(ticket: int, fraction: float) -> None:
    p = _pos(ticket)
    info = mt5.symbol_info(p.symbol)
    step = info.volume_step or 0.01
    vol = max(info.volume_min, round(p.volume * fraction / step) * step)
    _close_volume(ticket, round(vol, 8))


def cmd_close(ticket: int) -> None:
    p = _pos(ticket)
    _close_volume(ticket, p.volume)


def cmd_positions() -> None:
    out = [
        {
            "ticket": p.ticket,
            "symbol": p.symbol,
            "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume": p.volume,
            "open": p.price_open,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit,
        }
        for p in mt5.positions_get() or ()
    ]
    print(json.dumps(out))


def main(argv: list[str]) -> None:
    boot()
    try:
        cmd, *a = argv
        if cmd == "open":
            cmd_open(a[0], a[1], float(a[2]), float(a[3]), float(a[4]))
        elif cmd == "modify":
            cmd_modify(int(a[0]), float(a[1]), float(a[2]))
        elif cmd == "partial":
            cmd_partial(int(a[0]), float(a[1]))
        elif cmd == "close":
            cmd_close(int(a[0]))
        elif cmd == "positions":
            cmd_positions()
        else:
            sys.exit(f"unknown command {cmd!r}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main(sys.argv[1:])
