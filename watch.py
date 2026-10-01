#!/usr/bin/env python3
"""人気が急増しているコインを検出して Telegram に通知する。

データ源は 3 つ（sources/ 配下のアダプタ。どれも正規化行 rules.ROW_KEYS を返す）:
  GeckoTerminal  新規プール・trending の「発見」（実測の持続上限 約 5 コール/分）
  DexScreener    既知の全銘柄を 30 件ずつ一括更新（300 コール/分）。価格・出来高の主軸
  GMGN           保有者・スマートマネー・注目・リスクの「補強」（約 35 秒に 1 コール）

サブコマンド:
  cycle   launchd から連続で実行。巡回 → 判定 → Telegram 送信（dry_run 中は送らない）
  once    手動 1 回。送信せず、判定結果を標準出力に出す（cycle と同じだけ API を叩く）
  digest  日次まとめ（23:00 JST）。dry_run 中でも送る
  setup   Telegram の chat_id を取得して Keychain に保存し、テスト通知を送る
  status  稼働状況を標準出力に出す
  near    発火しなかったが近かった銘柄（しきい値の較正用）
  config  現在のしきい値を一覧
"""
import argparse
import bisect
import html
import json
import math
import os
import statistics
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import baseline  # noqa: E402
import db  # noqa: E402
import enrich  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
import score_log  # noqa: E402
from sources import dexscreener, geckoterminal, gmgn  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
# config.json（各自の設定・git 管理外）がまだ無ければ同梱の例を読む。書き込みは常に config.json へ
EXAMPLE_CONFIG_PATH = os.path.join(HERE, "config.example.json")
ALLOWED_CHAINS = ("sol", "bsc", "base", "eth", "robinhood", "arc")
HISTORY_HOURS = 2  # S2 の平常値が 75 分前まで要るので、判定に渡す履歴は 2 時間ぶん
DS_CALLS_PER_MIN = 270   # DexScreener の上限 300/分。高速レーンのぶんを残して撃つ
DS_STALE_SHARE = 0.2     # 1 分の枠のうち「最終更新が古い順」に回す割合
DEFAULTS = {
    "chains": ["bsc", "robinhood", "arc"],
    "dry_run": True,
    # size_any は「時価総額 / 24h 出来高 / 5 分出来高 の**どれか 1 つ**が下限以上」。
    # 小型が 5 分 +27% で撃ちまくって 100〜250 通/時になったため（2026-09-18）
    "filters": {"min_liquidity_usd": 20000, "max_market_cap_usd": 500000000,
                "min_holders": 30,
                "size_any": {"mcap": 100000, "vol24h": 300000, "vol5m": 30000}},
    "s1": {"pc1h_pct": 80, "pc5m_pct": 25, "min_buys_5m": 30, "cooldown_h": 2},
    "s2": {"min_vol5m_usd": 20000, "baseline_mult": 3, "baseline_from_min": 15,
           "baseline_to_min": 75, "no_baseline_vol5m_usd": 60000, "cooldown_h": 2},
    "s3": {"min_buys_5m": 300, "min_buyers_5m": 150, "buy_sell_ratio": 1.0, "cooldown_h": 2},
    "s4": {"window_min": 30, "pct": 15, "abs": 30, "cooldown_h": 2},
    "s5": {"min_smart": 5, "window_min": 30, "min_increase": 3, "cooldown_h": 6},
    "s6": {"window_min": 30, "ratio": 2, "min_visiting": 300, "cooldown_h": 6},
    "m1": {"ladder": [250000, 500000, 1000000, 3000000, 10000000, 30000000, 100000000],
           "max_age_days": 30},
    "m2": {"ladder": [3, 10, 30, 100]},
    "m3": {"ladder": [700000, 1000000, 3000000, 10000000, 30000000, 100000000],
           "max_age_days": 30},
    "m4": {"min_age_days": 30, "min_change_24h_pct": 200, "cooldown_h": 24},
    "milestone": {"min_pc1h_pct": 0, "min_vol5m_usd": 2000, "min_buys_5m": 5},
    "yabai": {"mcap": 1000000, "volume": 1000000, "x": 10, "heat": 4},
    # 即時に送るのは「やばい」か大きい銘柄だけ。残りは batch_minutes ごとに 1 通へまとめる
    # （batch_minutes=0 で全部即時）。max_pending 件たまったら待たずに送る。
    # immediate_cooldown_min: 直近にその銘柄を即時で送っていたら、やばい以外はまとめに回す
    # （1 銘柄がラダーを駆け上がると即時が 5 通続くため。2026-09-18 実測で即時の 25%）
    # legacy=false の間は旧ルール（S/M）を Telegram に送らない（events への記録は続ける）
    "notify": {"immediate": {"mcap": 300000, "vol24h": 700000},
               "immediate_cooldown_min": 10, "batch_minutes": 5, "max_pending": 15,
               "legacy": True},
    # EMS（Early Momentum Score）。カットは動かさず、調整は gain だけで行う
    "score": {"enabled": True, "notify": True, "gain": 1.0, "kappa": 0.5,
              "levels": {"WATCH": 60, "HOT": 70, "STRONG": 80, "EXTREME": 90},
              "cooldown_min": {"WATCH": 30, "HOT": 60, "STRONG": 120, "EXTREME": 360},
              "renotify_delta": 5,
              "min_liquidity_usd": 20000, "min_liquidity_new_usd": 10000,
              "new_token_min": 60, "fourmeme_grad_bnb": 18,
              "log_min_score": 30, "db_min_score": 40, "retention_days": 14,
              "api_chains": ["robinhood", "arc"], "wallets_file": "wallets.txt"},
    # 盛り上がり確定レーン（hot.py）。早さを捨てて「すでに盛り上がった銘柄」だけを
    # 2 周期連続の通過で確定する。何倍かは表示だけ。ピーク時価総額は size.peak_mcap_usd の
    # 下限と、出来高の代替（size.alt_*）にだけ使う（2026-09-25 本人決定）。
    # size.alt_* は「保有者が多ければ取引は少なめで可」の代替、burst の *_chg24_pct と holder_growth_pct は
    # 値幅・保有者増で通すときにも値上がりと伸び率を要る条件（暴落・横ばいの大型を落とす。2026-09-24）
    "hot": {"interval_sec": 600, "api_gap_sec": 12,
            "pulls": ["swaps", "volume", "chg100", "floor"],
            "size": {"swaps": 3000, "volume_usd": 300000, "holders": 300,
                     "alt_holders": 1000, "alt_swaps": 1500,
                     "peak_mcap_usd": 100000, "alt_volume_usd": 150000,
                     "alt_peak_mcap_usd": 300000},
            "burst": {"chg24_pct": 100, "big_swaps": 30000, "big_chg24_pct": 20,
                      "day_range_x": 2.0, "day_range_chg24_pct": 0,
                      "holder_growth": 300, "holder_growth_pct": 20, "holder_chg24_pct": 20},
            "confirm_passes": 2, "cooldown_hours": 48,
            "wallet_tags": ["smart_degen", "kol", "renowned", "top_renamed", "top_followed"],
            "wallet_exclude_tags": ["sandwich_bot", "dex_bot"],
            "near_limit": 5, "retention_days": 14},
    "gt": {"enabled": True, "max_calls_per_min": 5, "min_gap_seconds": 12,
           "block_seconds": 60, "trending_duration": "5m"},
    "ds": {"enabled": True, "workers": 6, "active_hours": 24},
    "gmgn": {"enabled": True, "interval_24h_every": 3},
    "snapshot_min_gap_seconds": 90,
    "snapshot_retention_days": 2,
}

# --- 設定 -----------------------------------------------------------------

def _config_source():
    return CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(_config_source(), encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  [config] 読み込み失敗（既定値で続行）: {e}", file=sys.stderr)
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def save_config(updates):
    """他のキーを保ったまま config.json を書き換える。

    両レーンが 60 秒ごとに読み直すので、書きかけを読ませないよう
    tmp → os.replace の原子的置換にする（bot からの /set が巡回中に走るため）。
    """
    try:
        with open(_config_source(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = json.loads(json.dumps(DEFAULTS))
    raw.update(updates)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


# --- データ源 --------------------------------------------------------------

# 同じ銘柄を複数の源が返したときの優先順（左が強い）。価格・出来高は速い方を採る。
SOURCE_PRIORITY = ("ds", "gt", "gmgn")
# 銘柄そのものの属性は GMGN が正しい（GT/DS のはプール生成時刻・"SYM / QUOTE" 表記）
FIELD_PRIORITY = {"created_ts": ("gmgn", "gt", "ds"),
                  "symbol": ("gmgn", "ds", "gt"),
                  "name": ("gmgn", "ds", "gt")}


def collect(bucket, rows):
    for r in rows:
        key = (r.get("chain"), r.get("address"))
        if key[0] and key[1]:
            bucket.setdefault(key, {})[r.get("source")] = r


def merge_sources(by_source):
    """源ごとの正規化行を 1 行にまとめる（各キー 優先順で最初に埋まっている値）。"""
    keys = set(rules.ROW_KEYS)
    for r in by_source.values():
        keys |= set(r)
    out = {}
    for k in keys:
        out[k] = None
        for s in FIELD_PRIORITY.get(k, SOURCE_PRIORITY):
            r = by_source.get(s)
            if r is not None and r.get(k) is not None:
                out[k] = r[k]
                break
    # 時価総額と「FDV で代用した印」は必ず同じ源から採る
    out["mcap"], out["mcap_is_fdv"] = None, False
    for s in SOURCE_PRIORITY:
        r = by_source.get(s)
        if r is not None and r.get("mcap") is not None:
            out["mcap"], out["mcap_is_fdv"] = r["mcap"], bool(r.get("mcap_is_fdv"))
            break
    out["source"] = "+".join(s for s in SOURCE_PRIORITY if s in by_source)
    return out


def gt_plan(chains, seq):
    """このサイクルで叩く GeckoTerminal のエンドポイント（20 秒周期＝3 サイクル/分が前提）。

    上限は回数だけでなく**間隔**で効く（12 秒に 1 回）。同じサイクル内で 2 発続けて撃つと
    分 5 コールに収まっていても 429 になったので（2026-09-18 実測）、1 サイクル 1 コールの
    持ち回りにした＝3/分・間隔は 17 秒以上。2 分（6 サイクル）の配分は
    主チェーンの trending 3 回・new_pools 1 回・他チェーンの trending 1 回ずつ
    （主チェーンの出来高上位は 4 分に 1 回。既知銘柄の 24h 出来高は DexScreener が毎サイクル返す）。
    """
    if not chains:
        return []
    primary, others = chains[0], chains[1:]
    slot = seq % 6
    if slot == 1:
        return [("new", primary)]              # four-meme 等の打ち上げ直後を拾う
    if slot == 3 and others:
        return [("trending", others[0])]
    if slot == 5:
        if seq % 12 == 11:
            return [("h24", primary)]
        if others:
            return [("trending", others[-1])]
    return [("trending", primary)]


def gt_used(conn, now):
    """今の 1 分間に GeckoTerminal を撃った回数。"""
    if int(rules.num(db.kv_get(conn, "gt_minute", 0)) or 0) != int(now // 60):
        return 0
    return int(rules.num(db.kv_get(conn, "gt_count", 0)) or 0)


def gt_take(conn, cfg, now):
    """GeckoTerminal を 1 コール撃ってよいか（間隔・分単位の予算・429 後のブロック）。"""
    gt = cfg.get("gt", {})
    if now < (rules.num(db.kv_get(conn, "gt_block_until", 0)) or 0):
        return False
    last = rules.num(db.kv_get(conn, "gt_last_ts", 0)) or 0
    if now - last < gt.get("min_gap_seconds", 12):
        return False
    used = gt_used(conn, now)
    if used >= gt.get("max_calls_per_min", 5):
        return False
    db.kv_set(conn, "gt_minute", int(now // 60))
    db.kv_set(conn, "gt_count", used + 1)
    db.kv_set(conn, "gt_last_ts", now)
    return True


def discover(conn, cfg, chains, seq, bucket):
    """GeckoTerminal で新規プール・trending を拾う。(コール数, 429 か, 当該分の累計)。"""
    gt = cfg.get("gt", {})
    calls, limited = 0, False
    now = int(time.time())
    if not gt.get("enabled", True):
        return 0, False, gt_used(conn, now)
    for kind, chain in gt_plan(chains, seq):
        now = int(time.time())
        if not gt_take(conn, cfg, now):
            break
        fetch = geckoterminal.FETCHERS[kind]
        rows, status = (fetch(chain, gt.get("trending_duration", "5m"))
                        if kind == "trending" else fetch(chain))
        calls += 1
        db.record_api(conn, now, f"gt {kind} {chain}", status == 200, 0)
        if status == 429:
            block = gt.get("block_seconds", 60)
            db.kv_set(conn, "gt_block_until", now + block)
            limited = True
            print(f"  [gt 429] {block} 秒は撃たない", file=sys.stderr, flush=True)
            break
        collect(bucket, rows)
    conn.commit()
    return calls, limited, gt_used(conn, int(time.time()))


def ds_targets(conn, cfg, chains, bucket, cap):
    """今サイクル DexScreener で引く [(chain, address), ...] と、枠に入らず見送った件数。

    並び順は「今サイクルの新顔 → 最終更新が古いぶん → 活動が新しい順」。枠で切るのは
    末尾（＝いちばん動いていない銘柄）。活動順だけで切ると静かな銘柄が永久に更新
    されないので、枠の DS_STALE_SHARE だけ「古い順」を先頭側に混ぜる。
    """
    since = time.time() - cfg.get("ds", {}).get("active_hours", 24) * 3600
    cap = max(0, cap)
    stale = db.stale_addresses(conn, since, chains,
                               max(1, int(cap * DS_STALE_SHARE))) if cap else []
    order = ([p for p in bucket if p[0] in chains]
             + stale + db.active_addresses(conn, since, chains))
    out, seen = [], set()
    for p in order:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:cap], len(out) - min(len(out), cap)


def ds_budget(conn, per_min, now):
    """今サイクル DexScreener に撃ってよいコール数。

    60 秒の移動窓で per_min を越えないことに加え、**前回撃ってからの経過秒に比例した
    ぶんだけ**撃つ。窓の空きを 1 サイクルで食い切ると次のサイクルが 0 コールになり、
    更新できる銘柄数が 3,400 → 12 と波打つため（2026-09-18 実測）。
    """
    window = per_min - db.api_calls_since(conn, now - 60, "ds ")
    last = db.last_api_ts(conn, "ds ")
    paced = per_min if last is None else per_min * (now - last) / 60.0
    return int(min(window, paced))


def refresh(conn, cfg, chains, bucket):
    """直近に見た全銘柄を DexScreener で一括更新する。(コール数, 429 か, 見送り数)。

    DexScreener は 300 コール/分。銘柄が増えると 1 サイクル 100 コール超 × 3 サイクル/分で
    上限を越えて 429 の波が出ていた（2026-09-18 実測）。今サイクルの枠に収まるぶんだけ引き、
    入らなかったぶんは次のサイクルに回す。
    """
    ds = cfg.get("ds", {})
    if not ds.get("enabled", True):
        return 0, False, 0
    now = time.time()
    left = ds_budget(conn, int(ds.get("calls_per_min", DS_CALLS_PER_MIN)), now)
    targets, skipped = ds_targets(conn, cfg, chains, bucket, left * dexscreener.MAX_ADDRS)
    by_chain = {}
    for chain, addr in targets:
        by_chain.setdefault(chain, []).append(addr)
    rows, calls, limited = dexscreener.fetch_many(by_chain, ds.get("workers", 6))
    if calls:
        db.record_api(conn, time.time(), f"ds tokens x{calls}", not limited, 0, calls)
    collect(bucket, rows)
    return calls, limited, skipped


def augment(conn, cfg, chains, bucket):
    """撃てるときだけ GMGN を 1 コール（保有者・スマートマネー・注目・リスクの補強）。"""
    g = cfg.get("gmgn", {})
    if not g.get("enabled", True) or not chains:
        return 0, 0
    # 共有バケット。通知時の画像取得ぶんを常に残すので巡回は BACKFILL_ROOM まで
    if not gmgn.has_room(conn, gmgn.BACKFILL_ROOM):
        return 0, 0
    i = int(rules.num(db.kv_get(conn, "gmgn_seq", 0)) or 0)
    chain = chains[i % len(chains)]
    every = max(1, int(g.get("interval_24h_every", 3)))
    interval = "24h" if (i // len(chains)) % every == every - 1 else "5m"
    db.kv_set(conn, "gmgn_seq", i + 1)
    try:
        rows, retries = gmgn.trending(conn, chain, interval)
    except RuntimeError as e:
        print(f"  [skip] gmgn {chain} {interval}: {e}", file=sys.stderr, flush=True)
        return 1, 0
    collect(bucket, rows)
    return 1, retries


# --- 表示 -----------------------------------------------------------------

def sanitize(s, limit=20):
    """name / symbol は攻撃者が自由に決められる文字列なので、制御文字を落として短く切る。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = s.strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return html.escape(s)


def gmgn_url(chain, address):
    return f"https://gmgn.ai/{chain}/token/{address}"


# DexScreener の URL だけチェーン名が違う（API の chainId はそのままで通る）
DS_URL_CHAIN = {"sol": "solana", "eth": "ethereum"}


def ds_url(chain, address):
    return f"https://dexscreener.com/{DS_URL_CHAIN.get(chain, chain)}/{address}"


def alert_links(row):
    """通知に付けるリンク [(表示名, URL), …]。launchpad が four.meme なら公式ページも。"""
    chain, addr = row["chain"], row["address"]
    links = [("GMGN", gmgn_url(chain, addr)), ("DexScreener", ds_url(chain, addr))]
    lp = str(row.get("launchpad") or "").strip().lower()
    # flap は個別トークンの公開 URL 形式が確認できていないので付けない
    if lp in ("four.meme", "fourmeme", "four_meme"):
        links.append(("four.meme", f"https://four.meme/token/{addr}"))
    return links


def _rate_pct(v):
    v = rules.num(v)
    return None if v is None else v * 100


def _ago_text(sec):
    d = rules.fmt_dur(sec)
    # 「1 分未満前」は日本語として崩れるのでそこだけ「前」を付けない
    return d if d == "1 分未満" else f"{d}前"


def _age_text(sec):
    return "作成 " + _ago_text(sec)


def _head_line(row, fired, now, token):
    """1 行目: 【やばい】$SYM  BSC · four.meme 63% ・ 作成 16 分前。"""
    sym = sanitize(row.get("symbol")) or "?"
    trans = sanitize(row.get("trans_symbol"))
    name = f"${sym}" + (f"({trans})" if trans and trans != sym else "")
    label = "【やばい】" if any(e.yabai for e in fired) else ""
    tail = [row["chain"].upper()]
    lp = sanitize(row.get("launchpad"), 24)
    if lp:
        prog = rules.num(row.get("launchpad_progress"))
        # 進捗は 0〜1 の比率。移行済み（1.0）は書いても情報がないので出さない
        tail.append(lp + (f" 進捗 {prog * 100:.0f}%" if prog is not None and 0 < prog < 1 else ""))
    created = rules.num(token.get("creation_ts")) or rules.num(row.get("created_ts"))
    if created:
        tail.append(_age_text(now - created))
    return f"<b>{label}{name}</b>  " + " · ".join(tail)


def _numbers_lines(row, now, cfg):
    """規模・5 分の動き・変化率の 3 行（取れた値だけ）。"""
    lines = []
    size = []
    mcap = rules.num(row.get("mcap"))
    if mcap is not None:
        # 時価総額が取れず FDV で代用したときは、そう書く（別物なので）
        size.append(f"{'FDV' if row.get('mcap_is_fdv') else '時価総額'} {rules.fmt_usd(mcap)}")
    liq = rules.num(row.get("liquidity"))
    if liq is not None:
        size.append(f"流動性 {rules.fmt_usd(liq)}")
    vol24h = rules.num(row.get("vol24h"))
    if vol24h is not None:
        size.append(f"24h 出来高 {rules.fmt_usd(vol24h)}")
    if size:
        lines.append(" ・ ".join(size))

    five = []
    buys, sells = rules.num(row.get("buys5m")), rules.num(row.get("sells5m"))
    if buys is not None and sells is not None:
        buyers = rules.num(row.get("buyers5m"))
        five.append(f"5 分: 買 {int(buys):,} / 売 {int(sells):,}"
                    + (f"（{int(buyers):,} 人）" if buyers is not None else ""))
    vol5m = rules.num(row.get("vol5m"))
    if vol5m is not None:
        five.append(f"出来高 {rules.fmt_usd(vol5m)}")
    holders = rules.num(row.get("holders"))
    if holders is not None:
        win = cfg.get("s4", {}).get("window_min", 30)
        base = rules.baseline_snapshot(row.get("_snapshots", []), now, win)
        b = rules.num(base.get("holders")) if base else None
        five.append(f"保有 {int(holders):,}"
                    + (f"({rules.fmt_pct((holders - b) / b * 100)}/{win} 分)" if b else ""))
    if five:
        lines.append(" ・ ".join(five))

    moves = []
    for key, lbl in (("pc1h", "1h"), ("pc24h", "24h")):
        v = rules.num(row.get(key))
        if v is not None:
            moves.append(f"{lbl} {rules.fmt_pct(v)}")
    for key, lbl in (("smart", "smart"), ("renowned", "KOL")):
        v = rules.num(row.get(key))
        if v:
            moves.append(f"{lbl} {int(v)}")
    vis = rules.num(row.get("visiting"))
    if vis:
        moves.append(f"注目 {int(vis):,}")
    if moves:
        lines.append(" ・ ".join(moves))
    return lines


def _gmgn_risk_parts(row, security):
    """GMGN 側のリスク値。GoPlus が無いチェーンではこれだけで「安全:」行を組む。"""
    parts = []
    top10 = _rate_pct(row.get("top10_rate"))
    if top10 is not None:
        parts.append(f"top10 {top10:.0f}%")
    for key, lbl in (("bundler_rate", "bundler"), ("bot_rate", "bot")):
        v = _rate_pct(row.get(key))
        if v is not None:
            parts.append(f"{lbl} {v:.0f}%")
    rug = rules.num(row.get("rug_ratio"))
    if rug:
        parts.append(f"rug {rug:.2f}")
    sniper = rules.num(row.get("sniper_count"))
    if sniper:
        parts.append(f"sniper {int(sniper)}")
    if rules.truthy(row.get("wash")):
        parts.append("wash あり")
    holders = enrich.holder_count(security)
    if holders is None:
        holders = rules.num(row.get("holders"))
    if holders is not None:
        parts.append(f"保有者 {int(holders):,}")
    return parts


def build_message(row, token, events, now, cfg, security=None):
    """即時通知の本文（HTML）。1 秒で「何が起きたか」が分かる並びにする。

    security は GoPlus の結果（enrich.security）。None なら「安全: 未取得」と出す。
    """
    fired = sorted([e for e in events if e.notify], key=lambda e: e.trigger)
    lines = [_head_line(row, fired, now, token)]
    lines += [f"{e.trigger}: {e.text}" for e in fired]
    lines.append("")
    lines += _numbers_lines(row, now, cfg)
    lines.append("")

    safe = enrich.security_parts(security) + _gmgn_risk_parts(row, security)
    lines.append("安全: " + " ・ ".join(safe))
    danger = enrich.danger_parts(security)
    if rules.truthy(row.get("honeypot")) and "honeypot" not in danger:
        danger.insert(0, "honeypot")
    if danger:
        lines.append("注意: " + " ・ ".join(danger))

    recent = sorted(set(row.get("_recent") or ()) & set(rules.TRIGGERS))
    heat = f"熱度 {fired[0].heat}/{rules.MAX_HEAT}" if fired else ""
    if recent:
        heat += f"（24h: {' '.join(recent)}）"
    src = row.get("source") or ""
    lines.append(heat + (f" ・ 源 {src}" if src else ""))
    lines += links_block(row)
    return "\n".join(lines)


SCORE_ICONS = {"WATCH": "👀", "HOT": "🔥", "STRONG": "🚨",
               "EXTREME": "💥"}
SCORE_PARTS = (("vol", "出来高"), ("buyers", "買い手"), ("flow", "流入"),
               ("accel", "加速"), ("wallet", "名簿"), ("price", "価格"))


def _price_text(p):
    """ミームは小数点以下が深いので、大きい値と小さい値で桁を変える。"""
    return f"${p:,.4f}" if p >= 0.01 else ("$" + f"{p:.10f}".rstrip("0"))


MAX_ENTRY_LINES = 6      # 名簿の行は多くても 6 人まで（caption 1024 字に収めるため）


def _entry_line(p, now):
    """名簿の人 1 人ぶんの行。名前は生の文字だけ（隠しリンクは Telegram が「開きますか」と聞くので付けない）。"""
    buy = rules.num(p.get("buy_usd")) or 0.0
    sell = rules.num(p.get("sell_usd")) or 0.0
    qty = rules.num(p.get("buy_qty")) or 0.0
    who = html.escape(str(p.get("name") or "")) or "?"
    handle = str(p.get("handle") or "").strip()
    if handle:
        who += f" @{html.escape(handle)}"
    parts = [who, f"買 ${buy:,.0f}"]
    if sell > 0:
        parts.append(f"売 ${sell:,.0f}")
    if qty > 0 and buy > 0:
        parts.append(f"平均 {_price_text(buy / qty)}")
    first = rules.num(p.get("first_buy_ts"))
    if first:
        parts.append(f"初回 {_ago_text(now - first)}")
    if sell >= buy > 0:
        parts.append("売り抜け済み")
    return " ・ ".join(parts)


def _crowd_parts(row, security):
    """保有者・上位集中・bundler・sniper・smart / KOL（取れた値だけ）。"""
    out = []
    holders = enrich.holder_count(security)
    if holders is None:
        holders = rules.num(row.get("holders"))
    if holders is not None:
        out.append(f"保有者 {int(holders):,}")
    top10 = _rate_pct(row.get("top10_rate"))
    if top10 is not None:
        out.append(f"上位10 {top10:.0f}%")
    for key, lbl in (("bundler_rate", "bundler"), ("bot_rate", "bot")):
        v = _rate_pct(row.get(key))
        if v:
            out.append(f"{lbl} {v:.0f}%")
    sniper = rules.num(row.get("sniper_count"))
    if sniper:
        out.append(f"sniper {int(sniper)}")
    for key, lbl in (("smart", "smart"), ("renowned", "KOL")):
        v = rules.num(row.get(key))
        if v:
            out.append(f"{lbl} {int(v)}")
    vis = rules.num(row.get("visiting"))
    if vis:
        out.append(f"注目 {int(vis):,}")
    return out


def build_score_message(row, res, feat, now, security=None):
    """EMS の通知本文（HTML）。1 行 1 項目で、上から「何が・どれくらい・誰が・どこで見るか」。

    点数の内訳は出さない（JSONL / scores.detail に残る）。リンクは生 URL を本文に置く。
    ボタンや <a href> の隠しリンクは Telegram アプリが「開きますか」と確認を挟むので使わない。
    security は GoPlus の結果（enrich.cached_security）。無ければ安全の行を省く。
    """
    level = res.get("level") or ""
    sym = sanitize(row.get("symbol")) or "?"
    name = sanitize(row.get("name"), 24)
    head = f"<b>{SCORE_ICONS.get(level, '')} {level} {rules.num(res.get('score')) or 0:.0f}点  ${sym}</b>"
    if name and name.lower() != sym.lower():
        head += f"  {name}"
    where = [str(row.get("chain") or "").upper()]
    lp = sanitize(row.get("launchpad"), 24)
    if lp:
        prog = rules.num(res.get("progress"))
        where.append(lp + (f" 卒業前 {prog * 100:.0f}%" if prog is not None and prog < 1 else ""))
    created = rules.num(row.get("created_ts"))
    if created:
        where.append(_age_text(now - created))
    lines = [head, " ・ ".join(where), ""]

    liq = rules.num(row.get("liquidity"))
    mcap = rules.num(row.get("mcap"))
    if mcap is not None:
        lines.append(f"{'FDV' if row.get('mcap_is_fdv') else '時価総額'} {rules.fmt_usd(mcap)}")
    if liq is not None:
        lines.append(f"流動性 {rules.fmt_usd(liq)}")
    price = rules.num(row.get("price"))
    if price is not None:
        pc = rules.num(feat.get("pc120"))
        moves = [f"2分 {rules.fmt_pct(pc)}"] if pc is not None else []
        for key, lbl in (("pc1h", "1h"), ("pc24h", "24h")):
            v = rules.num(row.get(key))
            if v is not None:
                moves.append(f"{lbl} {rules.fmt_pct(v)}")
        lines.append(f"価格 {_price_text(price)}" + (f"（{' / '.join(moves)}）" if moves else ""))

    w = int(rules.num(feat.get("flow_w")) or 30)
    label = f"{w}秒" if w < 60 else f"{w // 60}分"
    net = rules.num(feat.get(f"Net{w}")) or 0.0
    share = f"（流動性の {net / liq * 100:.0f}%）" if liq else ""
    lines.append(f"直近{label} 買い手 {int(rules.num(feat.get(f'U{w}')) or 0)}人"
                 f" ・ 買い {rules.fmt_usd(rules.num(feat.get(f'Vb{w}')) or 0)}"
                 f" ・ 純買い {rules.fmt_usd(net)}{share}")
    five = []
    buys, sells = rules.num(row.get("buys5m")), rules.num(row.get("sells5m"))
    if buys is not None and sells is not None:
        five.append(f"5分 買 {int(buys):,} / 売 {int(sells):,}")
    vol5m = rules.num(row.get("vol5m"))
    if vol5m is not None:
        five.append(f"出来高 {rules.fmt_usd(vol5m)}")
    vol24h = rules.num(row.get("vol24h"))
    if vol24h is not None:
        five.append(f"24h {rules.fmt_usd(vol24h)}")
    if five:
        lines.append(" ・ ".join(five))
    crowd = _crowd_parts(row, security)
    if crowd:
        lines.append(" ・ ".join(crowd))
    if security:
        lines.append("安全 " + " ・ ".join(enrich.security_parts(security)))
    danger = enrich.danger_parts(security)
    if rules.truthy(row.get("honeypot")) and "honeypot" not in danger:
        danger.insert(0, "honeypot")
    if rules.truthy(row.get("wash")):
        danger.append("wash あり")
    if danger:
        lines.append("⚠️ 注意 " + " ・ ".join(danger))

    # 名簿の人がこの銘柄に入っていれば 1 人 1 行（生取引のある高速レーンだけ付く）
    entries = res.get("entries") or []
    if entries:
        lines += ["", "👛 入ってる人（24h）"]
        lines += [_entry_line(p, now) for p in entries[:MAX_ENTRY_LINES]]
        if len(entries) > MAX_ENTRY_LINES:
            lines.append(f"…ほか {len(entries) - MAX_ENTRY_LINES} 人")

    lines.append("")
    lines += links_block(row)
    return "\n".join(lines)


def links_block(row):
    """本文末尾のリンク（生 URL を 1 行 1 本）とコピー用アドレス。

    生 URL は Telegram が自動でリンクにし、押しても確認ダイアログが出ない
    （ボタンや <a href> の隠しリンクは「開きますか」と聞かれる）。
    """
    return [u for _t, u in alert_links(row)] + [f"<code>{html.escape(str(row['address']))}</code>"]


def send_alert(conn, cfg, row, message, creds):
    """即時通知を 1 通送る。戻りは送信の実測（ok / photo / message_id / 時刻）。

    画像の**URL 探し**はしない（それはチェーンレーンの先回り＝chain/run.py の Prefetch が
    銘柄を見つけた時点で済ませておく）。ただし URL だけあって本体が手元に無いときは、
    ここで 2 秒だけ待って落とす。Telegram に URL を渡して取りに行かせると取りこぼしが
    多いため（2026-09-19 の実測で 1148 件中 311 件）、本体があれば multipart で送る。
    落とせなければ従来どおり URL を渡し、それも駄目ならテキストへ落とす。
    リンクは本文に生 URL で入っているのでボタンは付けない。caption は 1024 字までなので
    収まらなければ画像なしで送る（リンクや名簿の行を切り落とさないため）。
    """
    token, chat_id = creds
    out = {"ok": False, "photo": False, "message_id": None, "tg_date": None,
           "send_start_ts": time.time(), "sent_ts": None, "error": None}
    if not token or not chat_id:
        out["error"] = "credentials missing"
        return out
    photo = enrich.logo(conn, row["chain"], row["address"])
    if photo and "://" in photo:      # URL しかない＝本体を落としてから送る
        photo = enrich.store_logo_file(conn, row["chain"], row["address"], photo,
                                       timeout=enrich.ALERT_LOGO_TIMEOUT) or photo
    if photo and len(message) <= notify.CAPTION_LEN:
        res, err = notify.send_photo_ex(photo, message, token, chat_id)
        if res:
            return {**out, "ok": True, "photo": True, "sent_ts": time.time(),
                    "message_id": res.get("message_id"), "tg_date": res.get("date")}
        out["error"] = err
    res, err = notify.send_ex(message, token, chat_id)
    return {**out, "ok": bool(res), "sent_ts": time.time(),
            "message_id": (res or {}).get("message_id"), "tg_date": (res or {}).get("date"),
            "error": err or out["error"]}



def is_immediate(row, fired, cfg, last_ts=None, now=None):
    """即時に送るか。やばい判定か、そこそこ大きい銘柄だけ即時で、残りはまとめに回す。

    last_ts はその銘柄を最後に即時で送った時刻。immediate_cooldown_min 以内なら
    やばい以外はまとめに回す（同じ銘柄がラダーを駆け上がるときの連投を抑える）。
    """
    nb = cfg.get("notify", {})
    if not rules.num(nb.get("batch_minutes")):   # 0 なら全部即時
        return True
    if any(e.yabai for e in fired):
        return True
    cool = rules.num(nb.get("immediate_cooldown_min")) or 0
    if cool and last_ts is not None and now is not None and now - last_ts < cool * 60:
        return False
    imm = nb.get("immediate", {})
    for key in ("mcap", "vol24h"):
        v, gate = rules.num(row.get(key)), rules.num(imm.get(key))
        if v is not None and gate is not None and v >= gate:
            return True
    return False


def batch_text(conn, rows, now, chains=()):
    """まとめ 1 通の本文。同じ銘柄が複数回出ていたら 1 行にまとめる。

    並びは チェーン別（cfg の順）→ 時価総額の大きい順。1 行は
    「[チェーン] $SYM トリガー ・ 時価総額 ・ GMGN」だけに絞る（読み飛ばせる長さにする）。
    """
    groups = {}
    for r in rows:
        groups.setdefault((r["chain"], r["address"]), []).append(r)
    order = {c: i for i, c in enumerate(chains)}
    lines = []
    for (chain, addr), evs in groups.items():
        tok = db.latest(conn, chain, addr)
        sym = sanitize(tok.get("symbol")) or "?"
        trig = " ・ ".join(f"{e['trigger']}: {e['text']}"
                           for e in sorted(evs, key=lambda e: e["trigger"]) if e["text"])
        parts = [f"[{chain.upper()}] ${sym} {trig}".rstrip()]
        mcap = rules.num(tok.get("mcap"))
        if mcap is None:
            mcap = rules.num(tok.get("last_mcap"))
        if mcap is not None:
            parts.append(f"時価総額 {rules.fmt_usd(mcap)}")
        parts.append(f'<a href="{gmgn_url(chain, addr)}">GMGN</a>')
        lines.append(((order.get(chain, len(order)), -(mcap or 0)), " ・ ".join(parts)))
    lines.sort(key=lambda x: x[0])
    head = (f"pump-watch まとめ {time.strftime('%H:%M', time.localtime(now))}"
            f"（{len(groups)} 銘柄 / {len(rows)} 件）")
    return "\n".join([head] + [t for _, t in lines])


def flush_batch(conn, cfg, now, sender):
    """まとめ待ちが「最古が batch_minutes 以上前」か「max_pending 件以上」なら 1 通送る。"""
    nb = cfg.get("notify", {})
    minutes = rules.num(nb.get("batch_minutes")) or 0
    rows = db.pending_events(conn)
    if not rows:
        return 0, 0
    if (minutes and len(rows) < (rules.num(nb.get("max_pending")) or 15)
            and now - rows[0]["ts"] < minutes * 60):
        return 0, 0
    text = batch_text(conn, rows, now, cfg.get("chains", ()))
    ok = bool(sender) and notify.sent_ok(sender(text))
    # ドライランでも batched_ts は付ける（送っていたら何通になるかを後から数えるため）
    db.mark_batched(conn, [r["id"] for r in rows], now, ok)
    return 1, len(rows)


def fmt_ts(ts):
    ts = rules.num(ts)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "なし"


def chain_lane_line(conn, chain="bsc", now=None, stale_seconds=60):
    """高速レーン（chain_watch.py）の生存を 1 行で返す。

    別プロセスなので kv の心拍（10 秒ごと）を読むしかない。止まっていても /status が
    何も言わないと気付けないので、古い/無いときははっきり停止中と出す。
    """
    now = int(now or time.time())
    raw = db.kv_get(conn, f"chain_{chain}_status")
    try:
        beat = json.loads(raw) if raw else None
    except ValueError:
        beat = None
    if not beat:
        return f"chain {chain}: 停止中（記録なし）"
    age = now - int(beat.get("ts") or 0)
    if age >= stale_seconds:
        return f"chain {chain}: 停止中（最終 {max(age // 60, 1)} 分前）"
    return (f"chain {chain}: block {beat.get('block')}"
            f" / lag {beat.get('lag_s')}s / {age} 秒前")


def bot_lane_line(conn, now=None, stale_seconds=120):
    """常駐 bot（bot.py）の生存を 1 行で返す。別プロセスなので kv の最終受信時刻を読む。"""
    now = int(now or time.time())
    ts = rules.num(db.kv_get(conn, "bot_last_poll_ts"))
    if not ts:
        return "bot: 停止中（記録なし）"
    age = now - int(ts)
    if age >= stale_seconds:
        return f"bot: 停止中（最終受信 {max(age // 60, 1)} 分前）"
    return f"bot: 稼働中（最終受信 {age} 秒前）"


def status_text(conn, cfg):
    since = db.today_start()
    counts = db.trigger_counts(conn, since)
    detail = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "なし"
    hour = int(time.time()) - 3600
    digests, items = db.batch_counts(conn, hour)
    return "\n".join([
        "pump-watch 稼働状況",
        f"チェーン: {','.join(cfg['chains'])}",
        f"送信: {'OFF（ドライラン）' if cfg.get('dry_run') else 'ON'}",
        f"最終 cycle: {fmt_ts(db.kv_get(conn, 'last_cycle_ts'))}",
        f"当日の通知: {db.message_count(conn, since)} 通"
        f"（発火 {sum(counts.values())} 件: {detail}）",
        f"直近 1 時間: 即時 {db.message_count(conn, hour)} 通 / "
        f"まとめ {digests} 通（{items} 件）",
        f"当日の 429 待ち: {db.retry_count(conn, since)} 回",
        f"GMGN 枠: {gmgn.bucket_used(conn)}/{gmgn.NOTIFY_ROOM}"
        f"（直近 {gmgn.BUCKET_WINDOW} 秒）",
        chain_lane_line(conn),
        bot_lane_line(conn),
        hot_lane_line(conn),
    ])


def hot_lane_line(conn, now=None):
    """盛り上がり確定レーン（hot.py）の 1 行。hot は watch を import するのでここで読む。"""
    import hot
    return hot.lane_line(conn, now)


# --- Telegram コマンド ------------------------------------------------------

HELP = "\n".join([
    "pump-watch のコマンド（/menu でボタンを出す）",
    "/status - 稼働状況（チェーン・送信 ON/OFF・当日の通数・両レーンと bot の生存）",
    "/near [時間] - 発火しなかったが近かった銘柄を S1〜S6・M1〜M4 別に（既定 6h）",
    "/config - 現在のしきい値を一覧",
    "/set <キー> <値> - しきい値を変更（例: /set s3.min_buys_5m 200）",
    "/chains - 対象チェーンをボタンで切り替え（/chains bsc,arc と直接書いてもよい）",
    "/dryrun - 送信 ON/OFF をボタンで切り替え（/dryrun on|off でも可）",
    "/mark <アドレス> [メモ] - 目視で「これは良かった」を記録（rh: / arc: で chain 指定）",
    "/marks - 最近の /mark を 20 件",
    "/hot - 今日の盛り上がり（確定・継続・惜しい）を今の時刻で出す",
    "/score [件数] - 直近 10 分の EMS 上位（既定 10 件）",
    "/timeline <アドレス> - その銘柄の通知の時系列（作成→上昇→到達→送信）。/tl でも可",
    "/menu - 操作ボタンを出す",
    "/help - この一覧",
    "",
    "急増: S1 価格 / S2 出来高 / S3 殺到 / S4 保有者 / S5 スマートマネー / S6 注目",
    "到達: M1 時価総額 / M2 倍率 / M3 24h 出来高 / M4 古参復活",
    "配信: 全部即時（notify.batch_minutes=0）。分を設定すると小さい発火をまとめる",
])


def mark_command(conn, cfg, arg, now=None):
    """/mark <アドレス> [メモ]。chain は bsc 既定、rh: / arc: で切り替える。

    Tomato の目視メモ。後で JSONL と突き合わせて g とレベルを較正するための正解ラベル。
    """
    bits = arg.split(None, 1)
    if not bits:
        return "使い方: /mark 0xアドレス 覚書（rh: / arc: を頭に付けると chain 指定）"
    target, memo = bits[0], (bits[1].strip() if len(bits) > 1 else "")
    chain = "bsc"
    for prefix, name in (("rh:", "robinhood"), ("arc:", "arc"), ("bsc:", "bsc")):
        if target.lower().startswith(prefix):
            chain, target = name, target[len(prefix):]
            break
    address = target.strip().lower()
    if not address:
        return "アドレスが読めない。例: /mark 0xabc… 初動きれい"
    db.insert_mark(conn, chain, address, int(now or time.time()), memo)
    conn.commit()
    return f"記録した: {chain} {address}" + (f"\n{memo}" if memo else "")


def marks_text(conn, limit=20):
    rows = db.recent_marks(conn, limit)
    if not rows:
        return "まだ /mark は無い。"
    lines = [f"最近の /mark（{len(rows)} 件）"]
    for r in rows:
        lines.append(f"{fmt_ts(r['ts'])} {r['chain']} <code>{html.escape(r['address'])}</code>"
                     + (f" — {sanitize(r['memo'], 60)}" if r.get("memo") else ""))
    return "\n".join(lines)


def score_text(conn, cfg, limit=10, minutes=10, now=None):
    """直近 minutes 分に採点した銘柄の上位（銘柄ごとの最高点）。"""
    now = int(now or time.time())
    cuts = {**rules.LEVEL_CUTS, **((cfg.get("score") or {}).get("levels") or {})}
    rows = db.top_scores(conn, now - minutes * 60, limit)
    if not rows:
        return f"直近 {minutes} 分は採点なし。"
    lines = [f"直近 {minutes} 分の上位 {len(rows)} 件（EMS）"]
    for r in rows:
        value = rules.num(r.get("score")) or 0
        level = rules.level_of(value, cuts)
        lines.append(f"{value:.0f}点 {SCORE_ICONS.get(level, '·')} ${sanitize(r.get('symbol'))}"
                     f" {r['chain']} <code>{html.escape(r['address'])}</code>"
                     f" {fmt_ts(r['ts'])}")
    return "\n".join(lines)


def timeline_text(conn, chain, address, limit=5):
    """/timeline の本文。その銘柄に送った通知の時系列を新しい順に。

    chain=None なら全チェーンから拾う（アドレスだけ貼れば読めるように）。
    """
    address = str(address or "").strip()
    if not address:
        return "使い方: /timeline 0xアドレス（rh: / arc: で chain 指定）"
    if not address.startswith("So") and address.startswith("0x"):
        address = address.lower()
    rows = db.recent_alerts(conn, chain, address, limit)
    if not rows:
        return f"通知の記録なし: <code>{html.escape(address)}</code>"
    for r in rows:                      # 通知の後に分かった作成時刻は表示で補う
        if not r.get("created_ts"):
            tok = db.get_token(conn, r["chain"], address)
            r["created_ts"] = _int((tok or {}).get("creation_ts"))
    lines = [f"通知の記録 {len(rows)} 件 <code>{html.escape(address)}</code>"]
    for r in rows:
        lines.append("")
        lines.append(f"{fmt_ts(r['judged_ts'])} {r['chain']} ${sanitize(r.get('symbol'))}"
                     f" {r.get('level') or '?'} {rules.num(r.get('score')) or 0:.0f}点"
                     f"（{r.get('lane') or '?'}）")
        lines.append(html.escape(score_log.timeline_body(r)))
    return "\n".join(lines)


def timeline_command(conn, arg):
    """/timeline <アドレス>。chain 省略可、rh: / arc: / bsc: で指定。"""
    target = (arg or "").split()[0] if arg.strip() else ""
    chain = None
    for prefix, name in (("rh:", "robinhood"), ("arc:", "arc"), ("bsc:", "bsc")):
        if target.lower().startswith(prefix):
            chain, target = name, target[len(prefix):]
            break
    return timeline_text(conn, chain, target)


def set_chains(cfg, names):
    """(成否, 返信文)。知らないチェーン・空リストは何も変えない。"""
    bad = [c for c in names if c not in ALLOWED_CHAINS]
    if not names or bad:
        return False, f"使えるチェーン: {'/'.join(ALLOWED_CHAINS)}"
    cfg["chains"] = names
    save_config({"chains": names})
    return True, f"対象チェーンを {','.join(names)} に変更した"


def set_dry_run(cfg, on):
    cfg["dry_run"] = bool(on)
    save_config({"dry_run": cfg["dry_run"]})
    return ("送信 OFF にしました（ドライラン）。両レーンとも次の周期で反映（最大 30 秒）"
            if cfg["dry_run"] else
            "送信 ON にしました。両レーンとも次の周期で反映（最大 30 秒）")


def dispatch_command(conn, cfg, text):
    """コマンド 1 本を処理して返信文を返す（Telegram も getUpdates も触らない）。

    bot.py（常駐のロングポーリング）から呼ぶ。cycle 側では getUpdates を呼ばない
    ＝ Telegram は getUpdates の消費者を 1 つしか許さないため（409 衝突）。
    """
    parts = text.strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()
    arg = " ".join(parts[1:]).strip()
    if cmd == "/status":
        return status_text(conn, cfg)
    if cmd in ("/help", "/start", "/menu"):
        return HELP
    if cmd == "/chains":
        names = [c for c in arg.replace(" ", "").lower().split(",") if c]
        return set_chains(cfg, names)[1]
    if cmd == "/near":
        hours = rules.num(arg) or 6
        return near_report(conn, cfg, max(0.5, min(float(hours), 168)))
    if cmd == "/config":
        return config_text(cfg)
    if cmd == "/set":
        bits = arg.split(None, 1)
        if len(bits) != 2:
            return "使い方: /set s3.min_buys_5m 200"
        ok, reply, updates = set_config_value(cfg, bits[0], bits[1])
        if ok:
            save_config(updates)
        return reply
    if cmd == "/mark":
        return mark_command(conn, cfg, arg)
    if cmd == "/marks":
        return marks_text(conn)
    if cmd == "/hot":
        import hot                     # 循環 import を避けてここで読む（hot は watch を使う）
        return hot.digest_text(conn, cfg, time.time())
    if cmd == "/score":
        n = int(rules.num(arg) or 10)
        return score_text(conn, cfg, max(1, min(n, 30)))
    if cmd in ("/timeline", "/tl"):
        return timeline_command(conn, arg)
    if cmd == "/dryrun":
        a = arg.lower()
        if a in ("on", "off"):
            return set_dry_run(cfg, a == "on")
        return "使い方: /dryrun on | /dryrun off"
    return HELP


# --- 巡回 -----------------------------------------------------------------

def run_cycle(conn, cfg, *, send, echo, batch=True):
    """発見（GeckoTerminal）→ 更新（DexScreener）→ 補強（GMGN）→ 判定 → 通知。

    価格・出来高は毎サイクル DexScreener で全銘柄ぶん更新し、GMGN は無料枠の許す範囲で
    保有者・スマートマネー・注目・リスクを足すだけにする（GMGN だけだと 3 チェーンで 7 分周期）。
    """
    t0 = time.time()
    now = int(t0)
    chains = list(cfg["chains"])
    seq = int(rules.num(db.kv_get(conn, "cycle_seq", 0)) or 0)
    db.kv_set(conn, "cycle_seq", seq + 1)
    conn.commit()
    token, chat_id = notify.credentials() if send else (None, None)
    fired_total = sent_total = imm_total = batch_total = 0
    bucket = {}
    gt_calls, gt_limited, gt_min = discover(conn, cfg, chains, seq, bucket)
    ds_calls, ds_limited, ds_skip = refresh(conn, cfg, chains, bucket)
    gmgn_calls, retries_total = augment(conn, cfg, chains, bucket)
    if echo:
        for chain in chains:
            n = sum(1 for c, _ in bucket if c == chain)
            print(f"  {chain}: {n} 銘柄")

    sender = alert = None
    if send and token and chat_id:
        sender = lambda m: notify.send(m, token, chat_id)  # noqa: E731
        alert = lambda r, m: send_alert(conn, cfg, r, m, (token, chat_id))  # noqa: E731
    for by_source in bucket.values():
        row = merge_sources(by_source)
        message, fired, ok, pending = process_row(conn, cfg, row, now, sender, alert)
        fired_total += fired
        batch_total += pending
        if message:
            imm_total += 1
            sent_total += 1 if ok else 0
            if echo:
                print("-" * 60)
                print(message)
    digests, digest_items = flush_batch(conn, cfg, now, sender) if (
        batch and cfg.get("notify", {}).get("legacy", True)) else (0, 0)
    # 通知時にその場で取りに行かなくて済むよう、枠が余っているサイクルで画像を先に埋める
    enrich.backfill_logos(conn, chains, now)
    db.prune_snapshots(conn, now, cfg.get("snapshot_retention_days", 7))
    # 採点の記録（scores 表と日次 JSONL）は 1 日で数十万行になるので保存日数で切る
    keep_days = int(rules.num((cfg.get("score") or {}).get("retention_days")) or 14)
    db.prune_scores(conn, now, keep_days)
    db.prune_wallet_trades(conn, now, keep_days)
    db.prune_alerts(conn, now, keep_days)
    score_log.prune_logs(now, keep_days)
    enrich.prune_logo_files(now, keep_days)
    db.kv_set(conn, "last_cycle_ts", now)
    conn.commit()
    rows_total = len(bucket)
    calls_total = f"gt={gt_calls}({gt_min}/分) ds={ds_calls}/skip={ds_skip} gmgn={gmgn_calls}"
    if gt_limited or ds_limited:
        calls_total += " [429]"
    print(f"[{fmt_ts(now)}] cycle chains={','.join(chains) or '-'} rows={rows_total} "
          f"fired={fired_total} imm={imm_total} batch={batch_total} "
          f"digest={digests}({digest_items}) sent={sent_total} {calls_total} "
          f"429={retries_total} {time.time() - t0:.1f}s", flush=True)
    return rows_total, fired_total


def process_row(conn, cfg, row, now, sender=None, alert=None):
    """1 銘柄を判定して snapshots / events に書く。

    (通知本文, 発火数, 送信成否, まとめ待ちにした件数) を返す。同じサイクルで複数トリガーが
    出ても通知は 1 通なので、本文を持つ events 行も代表 1 行だけにする（行ごとに本文を
    持たせると件数が水増しされ、二重送信に見えていた 2026-09-18）。即時条件を満たさない
    発火は本文を組まず pending=1 で貯める。

    alert(row, 本文) を渡すと画像・安全確認・ボタン付きで送る（戻りは送信の実測 dict）。
    sender(本文)->bool は本文だけを送る簡易版（まとめ送信とテストが使う）。
    """
    chain, addr = row["chain"], row["address"]
    if not rules.passes_filters(row, cfg):
        return "", 0, False, 0
    # 5 分間まったく取引が無い銘柄は「生きている」と見なさない（更新対象を増やし続けないため）
    active = bool((rules.num(row.get("vol5m")) or 0) > 0
                  or (rules.num(row.get("buys5m")) or 0) > 0
                  or (rules.num(row.get("sells5m")) or 0) > 0)
    token_row = db.upsert_token(conn, chain, addr, {
        "symbol": row.get("symbol"), "name": row.get("name"),
        "launchpad": row.get("launchpad"), "creation_ts": _int(row.get("created_ts")),
        "price": rules.num(row.get("price")), "market_cap": rules.num(row.get("mcap")),
    }, now, active)
    # DexScreener 応答に画像が入っていたら、Telegram が取りに行けない URL より優先する
    if row.get("logo") and not enrich.photo_url(token_row.get("logo")):
        db.save_logo(conn, chain, addr, row["logo"])
        token_row["logo"] = row["logo"]
    history = db.get_history(conn, chain, addr, now,
                             HISTORY_HOURS)
    # 判定は毎サイクルするが、記録は間隔をあける（20 秒巡回で全銘柄ぶん書くと肥大するため。
    # ベースラインは 15〜75 分の窓なので 1 分半おきの記録で足りる）
    last_ts = history["snapshots"][-1]["ts"] if history["snapshots"] else None
    gap = rules.num(cfg.get("snapshot_min_gap_seconds")) or 0
    if last_ts is None or now - last_ts >= gap:
        db.insert_snapshot(conn, chain, addr, now, snapshot_of(row))
    row["_snapshots"] = history["snapshots"]
    row["_recent"] = history["recent_triggers"]
    events = rules.evaluate(token_row, history, row, now, cfg)
    fired = sorted((e for e in events if e.notify), key=lambda e: e.trigger)
    immediate = bool(fired) and is_immediate(
        row, fired, cfg, db.last_immediate_ts(conn, chain, addr), now)
    message = ""
    if immediate:
        message = build_message(row, token_row, events, now, cfg,
                                enrich.security(conn, chain, addr, now))
    # 旧ルールは notify.legacy=false の間は送らない（events への記録は従来どおり）
    legacy = cfg.get("notify", {}).get("legacy", True)
    ok = bool(message) and legacy and notify.sent_ok(
        alert(row, message) if alert else (sender and sender(message)))
    head = fired[0] if fired else None
    for e in events:
        carrier = e is head
        db.insert_event(conn, chain, addr, now, e, message if carrier else "", ok and carrier,
                        pending=1 if (e.notify and not immediate) else 0)
    sc = cfg.get("score") or {}
    if sc.get("enabled", True) and chain in (sc.get("api_chains") or ()):
        try:
            score_row(conn, cfg, row, now, alert, sender)
        except Exception as e:   # シャドー運用。旧ルールの巡回は止めない
            print(f"  [score] {row.get('symbol')} {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
    return message, len(fired), ok, 0 if immediate else len(fired)


def api_features(row, snaps, now):
    """API レーン（5 分集計しか無いチェーン）の簡易特徴量（§12.6）。

    窓は 5 分だけ。買い USD は「買い回数の比で出来高を割る」近似（源が内訳を返さない）。
    平常値はスナップショット履歴の分レート。窓のぶん（直近 5 分）は平常値から外す。
    """
    buys = rules.num(row.get("buys5m")) or 0.0
    sells = rules.num(row.get("sells5m")) or 0.0
    vol = rules.num(row.get("vol5m")) or 0.0
    both = buys + sells
    vb = vol * (buys / both) if both else 0.0
    vs = vol - vb
    u = rules.num(row.get("buyers5m"))
    u = buys if u is None else u
    cut = now - 300
    old = [x for x in snaps if (rules.num(x.get("ts")) or now) < cut]
    recent = [x for x in old if (rules.num(x.get("ts")) or 0) >= cut - 900]

    def rate(rows, field):
        vals = [rules.num(x.get(field)) for x in rows]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) / 5.0 if vals else 0.0

    floor_u, floor_v = baseline.floors(None)
    mu = max(rate(recent, "buyers5m") or rate(recent, "buys5m"),
             rate(old, "buyers5m") or rate(old, "buys5m"), floor_u)
    logs = [math.log1p(rules.num(x.get("vol5m")) / 5.0) for x in old
            if rules.num(x.get("vol5m")) is not None]
    med_raw = statistics.median(logs) if logs else 0.0
    med = max(med_raw, math.log1p(floor_v))
    mad = (statistics.median([abs(x - med_raw) for x in logs])
           if len(logs) >= 3 else 0.0)
    prev_u = rules.prev_value(old, "buyers5m")
    prev_u = rules.prev_value(old, "buys5m") if prev_u is None else prev_u
    created = rules.num(row.get("created_ts"))
    return {
        "wins": [300], "flow_w": 300, "flow_frac": 0.05,
        "address": row.get("address"), "symbol": row.get("symbol"),
        "launchpad": row.get("launchpad"), "price": rules.num(row.get("price")),
        "liq": rules.num(row.get("liquidity")),
        "age_min": (now - created) / 60.0 if created else None,
        "U300": u, "B300": buys, "Vb300": vb, "Vs300": vs, "Net300": vb - vs,
        "bsi": (vb - vs) / vol if vol else 0.0,
        "mu_rate": mu, "med": med, "mad": mad, "cohort": False,
        "pc120": rules.num(row.get("pc5m")),
        # 前スナップショットとの G の差が加速度（チェーンレーンの 30 秒リングの代わり）
        "g30_prev": rules.poisson_g(prev_u, mu * 5.0) if prev_u is not None else None,
    }


def score_row(conn, cfg, row, now, alert=None, sender=None):
    """API レーンの EMS 採点。bsc はチェーンレーンが採るのでここでは採らない。"""
    feat = api_features(row, row.get("_snapshots") or [], now)
    res = rules.score(feat, cfg, {
        "liquidity": row.get("liquidity"), "mcap": row.get("mcap"),
        "wash": row.get("wash"), "honeypot": row.get("honeypot"), "grad_usd": None,
        "snapshots": row.get("_snapshots") or [], "holders": row.get("holders"),
        "smart": row.get("smart"), "visiting": row.get("visiting"), "now": now})
    send = None
    if alert or sender:
        def send():
            # 本文は送ると決まってから組む。安全確認はキャッシュだけ見る（GoPlus を待たない）
            sec = enrich.cached_security(conn, row["chain"], row["address"])
            text = build_score_message(row, res, feat, now, sec)
            return alert(row, text) if alert else sender(text)
    res["decision"] = score_log.judge(
        conn, cfg, row["chain"], feat, res, now, send,
        {"liq": rules.num(row.get("liquidity")), "mcap": rules.num(row.get("mcap")),
         # タイムライン用（snapshots は JSONL には書かない＝SKIP_KEYS）
         "lane": "api", "created_ts": _int(row.get("created_ts")),
         "snapshots": row.get("_snapshots") or [], "data_ts": now,
         "price": rules.num(row.get("price"))})
    return res


def _int(v):
    v = rules.num(v)
    return None if v is None else int(v)


def snapshot_of(row):
    return {
        "price": rules.num(row.get("price")), "mcap": rules.num(row.get("mcap")),
        "buys5m": _int(row.get("buys5m")), "sells5m": _int(row.get("sells5m")),
        "buyers5m": _int(row.get("buyers5m")), "holders": _int(row.get("holders")),
        "smart": _int(row.get("smart")), "renowned": _int(row.get("renowned")),
        "liquidity": rules.num(row.get("liquidity")), "vol5m": rules.num(row.get("vol5m")),
        "vol24h": rules.num(row.get("vol24h")), "rank5m": _int(row.get("rank5m")),
        "rank24h": _int(row.get("rank24h")), "hot_level": _int(row.get("hot_level")),
        "visiting": _int(row.get("visiting")),
        "wash": 1 if rules.truthy(row.get("wash")) else 0,
        "pc5m": rules.num(row.get("pc5m")), "pc1h": rules.num(row.get("pc1h")),
        "pc24h": rules.num(row.get("pc24h")),
    }


# --- 近かった銘柄（しきい値の較正用） ---------------------------------------

def _near_baseline(snaps, ts_list, i, window_min, field):
    """i 番目から見て window_min 前（25〜45 分前相当）に最も近い過去スナップショット。"""
    s = snaps[i]
    target = s["ts"] - window_min * 60
    lo, hi = s["ts"] - (window_min + 15) * 60, s["ts"] - (window_min - 5) * 60
    j = bisect.bisect_right(ts_list, target)
    best = None
    for k in (j - 1, j):
        if 0 <= k < i and lo <= snaps[k]["ts"] <= hi and snaps[k].get(field) is not None:
            if best is None or abs(snaps[k]["ts"] - target) < abs(best["ts"] - target):
                best = snaps[k]
    return best


def _best_window_rate(snaps, window_min, field, rate_fn):
    """window_min 前と比べた到達率の最大値と (今の値, 前の値) を返す。"""
    ts_list = [s["ts"] for s in snaps]
    best = None
    for i, s in enumerate(snaps):
        v = rules.num(s.get(field))
        if v is None:
            continue
        b = _near_baseline(snaps, ts_list, i, window_min, field)
        bv = rules.num(b.get(field)) if b else None
        if bv is None:
            continue
        rate = rate_fn(v, bv)
        if rate is not None and (best is None or rate > best[0]):
            best = (rate, v, bv)
    return best


def _best_vol5m_rate(snaps, s2):
    """S2（平常比 3 倍）の到達率。平常値は 15〜75 分前の平均。"""
    pairs = [(s["ts"], rules.num(s.get("vol5m"))) for s in snaps
             if rules.num(s.get("vol5m")) is not None]
    if not pairs:
        return None
    ts_list = [t for t, _ in pairs]
    pre = [0.0]
    for _, v in pairs:
        pre.append(pre[-1] + v)
    lo_min, hi_min = s2.get("baseline_from_min", 15), s2.get("baseline_to_min", 75)
    gate_abs = s2.get("min_vol5m_usd", 20000)
    gate_new = s2.get("no_baseline_vol5m_usd", 60000)
    mult = s2.get("baseline_mult", 3)
    best = None
    for t, v in pairs:
        a = bisect.bisect_left(ts_list, t - hi_min * 60)
        b = bisect.bisect_right(ts_list, t - lo_min * 60)
        base = (pre[b] - pre[a]) / (b - a) if b > a else None
        gate = max(gate_abs, mult * base) if base is not None else gate_new
        if not gate:
            continue
        rate = v / gate
        if best is None or rate > best[0]:
            best = (rate, v, base)
    return best


def _pct(rate):
    return f"{rate * 100:.0f}%"


def _ladder_gate(ladder, used):
    """まだ消費していない一番下の段（全部消費済みなら None）。"""
    for s in sorted(ladder or []):
        if rules.skey(s) not in used:
            return s
    return None


NEAR_SECTIONS = ("S1", "S2", "S3", "S4", "S5", "S6", "M1", "M2", "M3", "M4")


def near_report(conn, cfg, hours=6, now=None):
    """「発火はしなかったが近かった」銘柄をトリガー別・チェーン別に並べる。

    S（急増）はその窓で発火済みの銘柄を外し、M（ラダー）は消費済みの段を外して
    次の段に対する到達率で並べる。しきい値の較正用。
    """
    now = int(now if now is not None else time.time())
    since = now - int(hours * 3600)
    fired = {}
    for r in conn.execute(
            'SELECT chain,address,"trigger" FROM events WHERE ts>=? AND notified=1', (since,)):
        fired.setdefault((r["chain"], r["address"]), set()).add(r["trigger"])
    used_stages = {}
    for r in conn.execute('SELECT chain,address,"trigger",stage FROM events WHERE stage<>""'):
        used_stages.setdefault((r["chain"], r["address"], r["trigger"]), set()).add(str(r["stage"]))
    snaps = {}
    for r in conn.execute("SELECT * FROM snapshots WHERE ts>=? ORDER BY ts ASC", (since,)):
        snaps.setdefault((r["chain"], r["address"]), []).append(dict(r))
    tokens = {(r["chain"], r["address"]): dict(r) for r in conn.execute("SELECT * FROM tokens")}

    s1, s2, s3, s4, s5, s6 = (cfg.get(k, {}) for k in ("s1", "s2", "s3", "s4", "s5", "s6"))
    m1, m2, m3, m4 = (cfg.get(k, {}) for k in ("m1", "m2", "m3", "m4"))
    chains = list(cfg["chains"])
    cand = {c: {k: [] for k in NEAR_SECTIONS} for c in chains}

    for (chain, addr), rows in snaps.items():
        if chain not in cand:
            continue
        # 規模の下限を通らない銘柄はそもそも判定していないので「近い」にも出さない
        if not rules.passes_filters(rows[-1], cfg):
            continue
        tok = tokens.get((chain, addr), {})
        head = f"${sanitize(tok.get('symbol')) or '?'}"
        tail = addr[:6]
        done = fired.get((chain, addr), set())
        put = lambda k, rate, text: cand[chain][k].append(  # noqa: E731
            (rate, f"{head} {text} ({_pct(rate)}) {tail}"))
        created = rules.num(tok.get("creation_ts"))
        age_days = (now - created) / 86400.0 if created else None

        if "S1" not in done:
            best = None
            for s in rows:
                # 買いが無い（取れていない）スナップショットは S1 が撃たないので数えない
                b = rules.num(s.get("buys5m"))
                if b is None or b < s1.get("min_buys_5m", 30):
                    continue
                for field, gate, lbl in (("pc1h", s1.get("pc1h_pct", 80), "1h"),
                                         ("pc5m", s1.get("pc5m_pct", 25), "5 分")):
                    v = rules.num(s.get(field))
                    if v is None or not gate:
                        continue
                    if best is None or v / gate > best[0]:
                        best = (v / gate, f"{lbl} {rules.fmt_pct(v)}")
            if best and best[0] > 0:
                put("S1", best[0], best[1])
        if "S2" not in done:
            d = _best_vol5m_rate(rows, s2)
            if d and d[0] > 0:
                base = f"平常 {rules.fmt_usd(d[2])}" if d[2] is not None else "平常値なし"
                put("S2", d[0], f"5 分 {rules.fmt_usd(d[1])}（{base}）")
        if "S3" not in done:
            # 買い手の実数が取れている銘柄はそちらで、無ければ買い回数で測る
            field, gate = (("buyers5m", s3.get("min_buyers_5m", 150))
                           if any(s.get("buyers5m") is not None for s in rows)
                           else ("buys5m", s3.get("min_buys_5m", 300)))
            best = max((s for s in rows if s.get(field) is not None),
                       key=lambda s: s[field], default=None)
            if best and gate:
                label = "買い手" if field == "buyers5m" else "買"
                sl = best.get("sells5m")
                put("S3", best[field] / gate, f"{label} {int(best[field]):,}"
                    + (f" / 売 {int(sl):,}" if sl is not None else ""))
        if "S4" not in done:
            win, g_pct, g_abs = s4.get("window_min", 30), s4.get("pct", 15), s4.get("abs", 30)
            d = _best_window_rate(rows, win, "holders", lambda v, bv: None if bv <= 0 else min(
                (v - bv) / bv * 100 / g_pct if g_pct else 0, (v - bv) / g_abs if g_abs else 0))
            if d and d[0] > 0:
                put("S4", d[0], f"{rules.fmt_pct((d[1] - d[2]) / d[2] * 100)}/{win} 分"
                    f"（{int(d[2]):,}→{int(d[1]):,}）")
        if "S5" not in done:
            inc, g_smart = s5.get("min_increase", 3), s5.get("min_smart", 5)
            d = _best_window_rate(rows, s5.get("window_min", 30), "smart",
                                  lambda v, bv: min((v - bv) / inc if inc else 0,
                                                    v / g_smart if g_smart else 0))
            if d and d[0] > 0:
                put("S5", d[0], f"+{int(d[1] - d[2])}（今 {int(d[1])}）")
        if "S6" not in done:
            ratio, g_vis = s6.get("ratio", 2), s6.get("min_visiting", 300)
            d = _best_window_rate(rows, s6.get("window_min", 30), "visiting",
                                  lambda v, bv: min((v / bv) / ratio if bv > 0 else v / ratio,
                                                    v / g_vis if g_vis else 0))
            if d and d[0] > 0:
                put("S6", d[0], f"{int(d[1]):,}（30 分前 {int(d[2]):,}）")

        young = age_days is not None and age_days <= m1.get("max_age_days", 30)
        if young:
            gate = _ladder_gate(m1.get("ladder"), used_stages.get((chain, addr, "M1"), set()))
            vals = [rules.num(s.get("mcap")) for s in rows if s.get("mcap") is not None]
            if gate and vals:
                put("M1", max(vals) / gate,
                    f"峰 {rules.fmt_usd(max(vals))} → {rules.fmt_usd(gate)} 段")
            gate = _ladder_gate(m3.get("ladder"), used_stages.get((chain, addr, "M3"), set()))
            vals = [rules.num(s.get("vol24h")) for s in rows if s.get("vol24h") is not None]
            if gate and vals:
                put("M3", max(vals) / gate,
                    f"24h 出来高 {rules.fmt_usd(max(vals))} → {rules.fmt_usd(gate)} 段")
        first_price = rules.num(tok.get("first_price"))
        gate = _ladder_gate(m2.get("ladder"), used_stages.get((chain, addr, "M2"), set()))
        vals = [rules.num(s.get("price")) for s in rows if s.get("price") is not None]
        if gate and vals and first_price:
            x = max(vals) / first_price
            put("M2", x / gate, f"峰 {rules.fmt_x(x)} → {rules.skey(gate)}x")
        if "M4" not in done and age_days is not None and age_days > m4.get("min_age_days", 30):
            gate = m4.get("min_change_24h_pct", 200)
            vals = [rules.num(s.get("pc24h")) for s in rows if s.get("pc24h") is not None]
            if gate and vals and max(vals) > 0:
                put("M4", max(vals) / gate, f"24h {rules.fmt_pct(max(vals))}")

    titles = [
        ("S1", f"S1 価格急騰（1h +{s1.get('pc1h_pct', 80):g}% か 5 分 +{s1.get('pc5m_pct', 25):g}%）"),
        ("S2", f"S2 出来高急増（5 分 {rules.fmt_usd(s2.get('min_vol5m_usd', 20000))} かつ平常比 {s2.get('baseline_mult', 3):g} 倍）"),
        ("S3", f"S3 殺到（買 {int(s3.get('min_buys_5m', 300))}）"),
        ("S4", f"S4 保有者急増（+{s4.get('pct', 15):g}% かつ +{int(s4.get('abs', 30))} 人）"),
        ("S5", f"S5 スマートマネー（+{int(s5.get('min_increase', 3))}）"),
        ("S6", f"S6 注目急増（{s6.get('ratio', 2):g} 倍かつ {int(s6.get('min_visiting', 300))}）"),
        ("M1", "M1 時価総額（次の段まで）"),
        ("M2", "M2 倍率（次の段まで）"),
        ("M3", "M3 24h 出来高（次の段まで）"),
        ("M4", f"M4 古参復活（24h +{m4.get('min_change_24h_pct', 200):g}%）"),
    ]
    lines = [f"近かった銘柄（直近 {hours:g}h・未発火のみ・到達率つき）"]
    for chain in chains:
        lines.append("")
        lines.append(f"[{chain.upper()}]")
        for key, title in titles:
            items = sorted(cand[chain][key], key=lambda x: x[0], reverse=True)[:5]
            if not items:
                continue
            lines.append(title)
            lines += [f"{i}. {text}" for i, (_, text) in enumerate(items, 1)]
    return "\n".join(lines)


# --- 設定の表示と変更 --------------------------------------------------------

def _fmt_cfg_value(v):
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        return ",".join(_fmt_cfg_value(x) for x in v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _cfg_lines(prefix, v):
    if isinstance(v, dict):
        out = []
        for k2, v2 in v.items():
            out += _cfg_lines(f"{prefix}.{k2}" if prefix else k2, v2)
        return out
    return [f"{prefix}={_fmt_cfg_value(v)}"]


def config_text(cfg):
    lines = ["現在の設定（/set <キー> <値> で変更）"]
    for k, v in cfg.items():
        lines += _cfg_lines(k, v)
    return "\n".join(lines)


def _parse_scalar(s):
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        return s


def parse_cfg_value(raw):
    """カンマ区切りはリスト、数値は数値、on/off は真偽、それ以外は文字列。"""
    s = str(raw).strip()
    low = s.lower()
    if low in ("on", "true", "yes"):
        return True
    if low in ("off", "false", "no"):
        return False
    if "," in s:
        return [_parse_scalar(x) for x in s.split(",") if x.strip()]
    return _parse_scalar(s)


def _same_type(old, new):
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return True
    if isinstance(old, list) and isinstance(new, list):
        return all(_same_type(old[0], x) for x in new) if old else True
    return type(old) is type(new)


def set_config_value(cfg, key, raw):
    """(成否, 返信文, 保存用 updates)。知らないキー・型違いは何も変えない。"""
    key = key.strip()
    if key.split(".")[0] == "chains":
        return False, "チェーンは /chains bsc,robinhood,arc で変える", None
    parts = key.split(".")
    holder = cfg
    for p in parts[:-1]:                      # notify.immediate.mcap のような入れ子も辿る
        if not isinstance(holder, dict) or not isinstance(holder.get(p), dict):
            holder = None
            break
        holder = holder[p]
    if holder is None or parts[-1] not in holder or isinstance(holder[parts[-1]], dict):
        return False, f"知らないキー: {key}（/config で一覧）", None
    old = holder[parts[-1]]
    new = parse_cfg_value(raw)
    if not _same_type(old, new):
        return False, (f"型が違う: {key} は今 {_fmt_cfg_value(old)}"
                       f"（{'リスト' if isinstance(old, list) else type(old).__name__}）"), None
    holder[parts[-1]] = new
    updates = {parts[0]: cfg[parts[0]]}
    return True, f"{key}: {_fmt_cfg_value(old)} → {_fmt_cfg_value(new)}", updates


# --- 日次まとめ ------------------------------------------------------------

def digest_text(conn, cfg, now):
    """直近 24h に通知した銘柄をチェーンごとに 熱度 → ピーク時価総額 順で並べる。"""
    agg = {}
    for r in conn.execute(
        'SELECT chain,address,"trigger",heat FROM events WHERE ts>=? AND notified=1',
        (int(now - 86400),),
    ):
        a = agg.setdefault((r["chain"], r["address"]), {"triggers": set(), "heat": 0})
        a["triggers"].add(r["trigger"])
        a["heat"] = max(a["heat"], r["heat"] or 0)
    by_chain = {}
    for (chain, addr), a in agg.items():
        t = conn.execute("SELECT * FROM tokens WHERE chain=? AND address=?",
                         (chain, addr)).fetchone()
        if not t:
            continue
        a.update(chain=chain, address=addr, token=dict(t),
                 peak_mcap=rules.num(t["peak_mcap"]) or 0.0)
        by_chain.setdefault(chain, []).append(a)

    date = time.strftime("%Y-%m-%d", time.localtime(now))
    lines = [f"pump-watch 日次まとめ {date}（直近 24h）"]
    for chain in cfg["chains"]:
        items = sorted(by_chain.get(chain, []),
                       key=lambda a: (a["heat"], a["peak_mcap"]), reverse=True)[:5]
        lines.append("")
        lines.append(f"[{chain.upper()}] 通知 {len(by_chain.get(chain, []))} 銘柄")
        if not items:
            lines.append("  なし")
            continue
        for i, a in enumerate(items, 1):
            t = a["token"]
            sym = sanitize(t.get("symbol")) or "?"
            peak_x = ""
            if rules.num(t.get("first_price")) and rules.num(t.get("peak_price")):
                peak_x = f"（初認識比 {rules.fmt_x(t['peak_price'] / t['first_price'])}）"
            cur = rules.num(t.get("last_mcap"))
            drop = ""
            if cur is not None and a["peak_mcap"]:
                drop = f" ・ 現在 {rules.fmt_usd(cur)}（{rules.fmt_pct((cur - a['peak_mcap']) / a['peak_mcap'] * 100)}）"
            lines.append(f"{i}. ${sym} 熱度 {a['heat']}/{rules.MAX_HEAT} ・ "
                         f"ピーク {rules.fmt_usd(a['peak_mcap'])}{peak_x}{drop}")
            lines.append(f"   {' '.join(sorted(a['triggers']))} ・ {gmgn_url(chain, a['address'])}")

    since = db.today_start(now)
    counts = db.trigger_counts(conn, since)
    detail = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "なし"
    lines += ["", f"当日の通知: {db.message_count(conn, since)} 通"
              f"（発火 {sum(counts.values())} 件: {detail}）",
              f"当日の 429 待ち: {db.retry_count(conn, since)} 回"]
    return "\n".join(lines)


# --- サブコマンド ----------------------------------------------------------

def cmd_cycle(args):
    conn = db.connect()
    # 自動コミット。既定のままだと最初の書き込み（api_calls の記録）で始まった
    # トランザクションがサイクル終了まで開きっぱなしになり、DexScreener 等の HTTP を
    # 待つあいだ数秒〜十数秒も書き込みロックを握って高速レーンと bot を止めていた。
    conn.isolation_level = None
    cfg = load_config()
    run_cycle(conn, cfg, send=not cfg.get("dry_run", True), echo=False)
    conn.close()


def cmd_once(args):
    conn = db.connect()
    cfg = load_config()
    # 手動の 1 回きりなので、常駐側が貯めたまとめ待ちには手を付けない
    run_cycle(conn, cfg, send=False, echo=True, batch=False)
    conn.close()


def cmd_status(args):
    conn = db.connect()
    print(status_text(conn, load_config()))
    conn.close()


def cmd_near(args):
    conn = db.connect()
    print(near_report(conn, load_config(), args.hours))
    conn.close()


def cmd_config(args):
    print(config_text(load_config()))


def cmd_digest(args):
    conn = db.connect()
    text = digest_text(conn, load_config(), int(time.time()))
    token, chat_id = notify.credentials()
    # まとめはドライラン中でも送る（1 日 1 通なので通知過多にならない）
    if token and chat_id and notify.send(text, token, chat_id):
        print(f"[{fmt_ts(time.time())}] digest sent ({len(text)} 文字)", flush=True)
    else:
        print(text)
    conn.close()


SETUP_HELP = "\n".join([
    "TELEGRAM_BOT_TOKEN が Keychain に無い。先に本人が次をやる:",
    "  1. Telegram で @BotFather に /newbot を送り、bot 名を決めてトークンを受け取る",
    "  2. ターミナルで（値は対話入力される）:",
    "     security add-generic-password -U -s TELEGRAM_BOT_TOKEN -a shared -w",
    "  3. 作った bot を開いて /start を 1 通送る",
    "  4. もう一度 ./run.sh setup を実行する",
])


def cmd_setup(args):
    token = notify.keychain("TELEGRAM_BOT_TOKEN")
    if not token:
        print(SETUP_HELP)
        sys.exit(2)
    updates, _err = notify.get_updates(token, 0)
    chat = None
    for u in reversed(updates):
        msg = u.get("message") or {}
        if (msg.get("text") or "").strip().lower().startswith("/start"):
            chat = msg.get("chat")
            break
    if chat is None:
        for u in reversed(updates):
            chat = (u.get("message") or {}).get("chat")
            if chat:
                break
    if not chat:
        print("bot にメッセージが届いていない。Telegram で bot に /start を送ってからやり直す。")
        sys.exit(2)
    import subprocess
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U",
                        "-s", "TELEGRAM_CHAT_ID", "-a", "shared", "-w", str(chat.get("id"))])
    if r.returncode != 0:
        print("Keychain への保存に失敗した。")
        sys.exit(1)
    who = chat.get("username") or chat.get("first_name") or chat.get("type", "chat")
    print(f"TELEGRAM_CHAT_ID を Keychain に保存した（宛先: {who}）。テスト通知を送る。")
    ok = notify.send("pump-watch のテスト通知。これが届いていれば通知設定は完了。\n"
                     "bot を起動したら /menu でボタンが出る。",
                     token, str(chat.get("id")))
    print("テスト通知: 送信成功" if ok else "テスト通知: 送信失敗（上のエラーを確認）")


def cmd_bot(args):
    # bot.py は watch.py を import するので、循環を避けてここで読み込む
    import bot
    bot.run()


def main():
    p = argparse.ArgumentParser(description="GMGN を巡回して盛り上がり銘柄を通知する")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cycle", help="2 分ごとの巡回（launchd 用）").set_defaults(fn=cmd_cycle)
    sub.add_parser("once", help="手動 1 回（送信せず標準出力）").set_defaults(fn=cmd_once)
    sub.add_parser("digest", help="日次まとめを Telegram に送る").set_defaults(fn=cmd_digest)
    sub.add_parser("setup", help="Telegram の chat_id を取得して保存").set_defaults(fn=cmd_setup)
    sub.add_parser("status", help="稼働状況").set_defaults(fn=cmd_status)
    near = sub.add_parser("near", help="条件を満たさなかったが近かった銘柄")
    near.add_argument("hours", nargs="?", type=float, default=6)
    near.set_defaults(fn=cmd_near)
    sub.add_parser("config", help="現在のしきい値を一覧").set_defaults(fn=cmd_config)
    sub.add_parser("bot", help="Telegram の常駐 bot（ボタン操作。launchd 用）").set_defaults(fn=cmd_bot)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
