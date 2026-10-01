"""GMGN（`gmgn` ラッパー経由）。保有者数・スマートマネー・KOL・注目・リスク・launchpad の唯一の供給元。

無料枠は約 35 秒に 1 コールずつ補充されるバケット（バースト容量 6 以上）なので、
**直近 210 秒の呼び出し回数**で残量を測る共有バケットにした。API レーンの巡回・
通知時の画像取得・高速レーンの通知は別プロセスだが同じ枠を食うので、
`api_calls` 表を数えることで 1 つの枠として扱う（2026-09-18）。
価格・出来高は GeckoTerminal / DexScreener の方が速いので、ここの値は他が持っていないときだけ使う。
"""
import contextlib
import io
import json
import subprocess
import sys
import time

import db
import rules
from gm import call as gmgn_call  # 429 自動待機つきの共通ラッパー（リポジトリ直下の gm.py）

BUCKET_WINDOW = 210   # この秒数ぶんの呼び出しを数える（補充 約 1/35 秒 × 6 枠）
NOTIFY_ROOM = 6       # 通知のための取得はここまで撃ってよい
BACKFILL_ROOM = 3     # 巡回・補完はここまで（通知ぶんの余裕を常に残す）
CMD_PREFIX = "gmgn "  # api_calls.cmd の接頭辞。共有バケットはこれを数える

# 5m / 24h のどの行からでも拾ってよい共通フィールド（先に埋まった値を優先）
COMMON_FIELDS = (
    "symbol", "name", "trans_symbol", "launchpad", "launchpad_platform", "launchpad_status",
    "launchpad_progress",
    "price", "market_cap", "liquidity", "holder_count", "smart_degen_count",
    "renowned_count", "hot_level", "visiting_count", "creation_timestamp", "open_timestamp",
    "top_10_holder_rate", "rug_ratio", "bundler_rate", "dev_team_hold_rate", "bot_degen_count",
    "bot_degen_rate", "sniper_count", "rat_trader_amount_rate", "entrapment_ratio",
    "is_wash_trading", "is_honeypot", "history_highest_market_cap",
)


def bucket_used(conn, now=None):
    """共有バケットの使用量（直近 BUCKET_WINDOW 秒の gmgn 呼び出し回数）。"""
    now = time.time() if now is None else now
    return db.api_calls_since(conn, now - BUCKET_WINDOW, CMD_PREFIX)


def has_room(conn, limit, now=None):
    """残量が limit 未満なら True（待たずに判断するだけ）。"""
    return bucket_used(conn, now) < limit


def call(conn, args):
    """gmgn ラッパーを 1 回叩き、429 リトライ回数を api_calls に記録する。"""
    buf = io.StringIO()
    err = None
    data = None
    try:
        with contextlib.redirect_stderr(buf):
            data = gmgn_call(args)
    except RuntimeError as e:
        err = e
    captured = buf.getvalue()
    if captured:
        sys.stderr.write(captured)
        sys.stderr.flush()
    retries = captured.count("[retry")
    db.record_api(conn, time.time(), CMD_PREFIX + " ".join(args), err is None, retries)
    conn.commit()
    if err:
        raise err
    return data, retries


def call_once(conn, args, timeout=3):
    """再試行なしで 1 回だけ叩く（サイクルを止めたくないところ用。待つくらいなら諦める）。

    gm.call は 429 のたびに 20 秒級で待つので、20 秒周期の巡回からは使えない。
    """
    ok, data = False, None
    try:
        p = subprocess.run(["gmgn", *args, "--raw"], capture_output=True, text=True,
                           timeout=timeout)
        out = p.stdout.strip()
        if p.returncode == 0 and out.startswith(("{", "[")):
            data = json.loads(out)
            ok = True
    except (subprocess.TimeoutExpired, OSError, ValueError):
        data = None
    db.record_api(conn, time.time(), CMD_PREFIX + " ".join(args), ok, 0)
    return data if ok else None


def token_info(conn, chain, address, timeout=3):
    """`gmgn token info` の data 部（logo / launchpad_progress 等）。取れなければ None。"""
    payload = call_once(conn, ["token", "info", "--chain", chain, "--address", address],
                        timeout=timeout)
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    return payload if payload.get("address") or payload.get("logo") else None


def trending_rows(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    rank = data.get("rank") if isinstance(data, dict) else data
    if not isinstance(rank, list):
        return []
    return [r for r in rank if isinstance(r, dict) and r.get("address")]


def norm_addr(chain, addr):
    a = str(addr or "").strip()
    return a if chain == "sol" else a.lower()


def merge_row(m, r, kind):
    for k in COMMON_FIELDS:
        if m.get(k) is None and r.get(k) is not None:
            m[k] = r.get(k)
    if m.get("pc5m") is None:
        m["pc5m"] = rules.num(r.get("price_change_percent5m"))
    if m.get("pc1h") is None:
        m["pc1h"] = rules.num(r.get("price_change_percent1h"))
    if kind == "5m":
        # 回数・出来高は interval ごとの値なので 5m 行からしか取らない
        # （24h 行の buys / volume を 5 分の値として扱わないため）
        m["buys"] = rules.num(r.get("buys"))
        m["sells"] = rules.num(r.get("sells"))
        m["vol5m"] = rules.num(r.get("volume"))
        m["rank5m"] = rules.num(r.get("rank"))
        if r.get("price_change_percent") is not None:
            m["pc5m"] = rules.num(r.get("price_change_percent"))
    elif kind == "24h":
        m["volume24h"] = rules.num(r.get("volume"))
        m["rank24h"] = rules.num(r.get("rank"))
        m["pc24h"] = rules.num(r.get("price_change_percent"))


def trending(conn, chain, interval="5m", limit=100):
    """1 コールで 1 チェーンの trending を取り、正規化行のリストにする。"""
    payload, retries = call(conn, ["market", "trending", "--chain", chain,
                                   "--interval", interval, "--limit", str(limit)])
    merged = {}
    for r in trending_rows(payload):
        key = norm_addr(chain, r["address"])
        m = merged.setdefault(key, {"chain": chain, "address": key})
        merge_row(m, r, interval)
    return [normalize(chain, m) for m in merged.values()], retries


def normalize(chain, m):
    """GMGN の生行を、データ源に依存しない正規化行（rules.ROW_KEYS）にする。

    取れない値は None。rules は None の指標を使うトリガーを評価しない。
    リスク系（top10 / rug / bundler / bot）は GMGN にしかないので通知本文の表示専用。
    """
    row = {k: None for k in rules.ROW_KEYS}
    row.update({
        "chain": chain, "address": m.get("address"), "source": "gmgn",
        "symbol": m.get("symbol"), "name": m.get("name"),
        "price": rules.num(m.get("price")), "mcap": rules.num(m.get("market_cap")),
        "mcap_is_fdv": False,
        "liquidity": rules.num(m.get("liquidity")),
        "buys5m": rules.num(m.get("buys")), "sells5m": rules.num(m.get("sells")),
        "vol5m": rules.num(m.get("vol5m")), "vol24h": rules.num(m.get("volume24h")),
        "pc5m": rules.num(m.get("pc5m")), "pc1h": rules.num(m.get("pc1h")),
        "pc24h": rules.num(m.get("pc24h")),
        "holders": rules.num(m.get("holder_count")), "smart": rules.num(m.get("smart_degen_count")),
        "renowned": rules.num(m.get("renowned_count")),
        "visiting": rules.num(m.get("visiting_count")),
        "created_ts": rules.num(m.get("creation_timestamp")) or rules.num(m.get("open_timestamp")),
        "launchpad": m.get("launchpad") or m.get("launchpad_platform"),
        "wash": rules.truthy(m.get("is_wash_trading")),
        "honeypot": rules.truthy(m.get("is_honeypot")),
    })
    row.update({
        "trans_symbol": m.get("trans_symbol"), "rank5m": rules.num(m.get("rank5m")),
        "rank24h": rules.num(m.get("rank24h")), "hot_level": rules.num(m.get("hot_level")),
        "top10_rate": rules.num(m.get("top_10_holder_rate")),
        "rug_ratio": rules.num(m.get("rug_ratio")),
        "bundler_rate": rules.num(m.get("bundler_rate")),
        "bot_rate": rules.num(m.get("bot_degen_rate")),
        "sniper_count": rules.num(m.get("sniper_count")),
        "launchpad_progress": rules.num(m.get("launchpad_progress")),
    })
    return row
