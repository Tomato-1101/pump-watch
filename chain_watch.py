#!/usr/bin/env python3
"""オンチェーン高速レーンの入口（BSC）。

API レーン（watch.py cycle）はデータ源そのものが 30〜60 秒遅れるので「条件を満たしてから
10 秒以内」には届かない。こちらは公開 RPC でブロックを直接追い、PancakeSwap V2/V3・
four.meme・flap の Swap を自前で集計して 2 秒ごとに判定する。判定ルール・events・
Telegram の本文と 2 段配信は API レーンと共有する（rules.py / db.py / watch.py を import）。

  chain_watch.py bsc                 launchd から常駐（config.json の dry_run に従う）
  chain_watch.py bsc --seconds 600   10 分だけ回して終わる（実測用）
  chain_watch.py verify --seconds 300  集計値を DexScreener と突き合わせて表を出す
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from chain import run as chain_run  # noqa: E402
import rules  # noqa: E402
from sources import dexscreener  # noqa: E402


def cmd_bsc(args):
    chain_run.main("bsc", seconds=args.seconds, no_send=args.no_notify)


def _ds_row(chain, address):
    rows, _ = dexscreener.fetch(chain, [address])
    best = None
    for r in rows:
        if r["address"] != address:
            continue
        if best is None or (r.get("liquidity") or 0) > (best.get("liquidity") or 0):
            best = r
    return best


def _diff(a, b):
    a, b = rules.num(a), rules.num(b)
    if a is None or b is None:
        return "-"
    if not b:
        return "n/a"
    return f"{(a / b - 1) * 100:+.0f}%"


def cmd_verify(args):
    """N 秒ぶん集めてから、出来高上位の銘柄をチェーン集計 vs DexScreener で並べる。"""
    _stats, aggregate = chain_run.main("bsc", seconds=args.seconds, no_send=True)
    now = int(time.time())
    rows = [r for r in (aggregate.row(a, now) for a in aggregate.active_since(now - 900))
            if r and r.get("price")]
    rows.sort(key=lambda r: -(r.get("vol5m") or 0))
    print("\n=== チェーン集計 vs DexScreener（5 分窓・価格・FDV）===")
    for row in rows[:args.top]:
        ds = _ds_row("bsc", row["address"])
        if not ds:
            print(f"{row.get('symbol')} {row['address']}: DexScreener に無し")
            continue
        print(f"\n{row.get('symbol') or ds.get('symbol')} {row['address']} ({row.get('dex')})")
        for label, mine, theirs in (
                ("5 分 買", row.get("buys5m"), ds.get("buys5m")),
                ("5 分 売", row.get("sells5m"), ds.get("sells5m")),
                ("5 分 出来高", row.get("vol5m"), ds.get("vol5m")),
                ("価格", row.get("price"), ds.get("price")),
                ("FDV", row.get("mcap"), ds.get("mcap"))):
            print(f"  {label:<10} chain={mine!s:<14} ds={theirs!s:<14} 差={_diff(mine, theirs)}")


def main():
    p = argparse.ArgumentParser(description="BSC のオンチェーン高速レーン")
    sub = p.add_subparsers(dest="cmd", required=True)
    bsc = sub.add_parser("bsc", help="常駐（launchd 用）")
    bsc.add_argument("--seconds", type=float, default=None, help="この秒数で終了する")
    bsc.add_argument("--no-notify", action="store_true", help="dry_run に関係なく送らない")
    bsc.set_defaults(fn=cmd_bsc)
    ver = sub.add_parser("verify", help="集計値を DexScreener と突き合わせる")
    ver.add_argument("--seconds", type=float, default=300)
    ver.add_argument("--top", type=int, default=3)
    ver.set_defaults(fn=cmd_verify)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
