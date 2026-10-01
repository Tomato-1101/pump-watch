#!/usr/bin/env python3
"""盛り上がり確定レーン。「すでに盛り上がった銘柄」を取りこぼしなく・誤検出なく確定する。

早さは要らない（10 秒以内は既存の 2 レーンの仕事）。10 分ごとに GMGN の trending 24h を
4 とおりの並びで取り、規模（取引数・出来高・保有者数・その日のピーク時価総額）と勢いの
両方を満たす銘柄が 2 周期連続で通過したときだけ「確定」として Telegram に 1 通送る。
何倍になったかは本文に出すだけで判定には使わない。ピーク時価総額は規模の下限と出来高の
代替にだけ使う（2026-09-25 Tomato 決定）。

  hot.py cycle    10 分ごとの巡回（launchd 用）。確定したら通知し、日次の集計を進める
  hot.py digest   23:00 JST の日次まとめ（確定・継続・惜しい）。ドライラン中でも送る
  hot.py judge    DB に溜めた最新観測を判定して表に出すだけ（API を叩かない・較正用）
"""
import argparse
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
import watch  # noqa: E402
from chain.run import load_wallets  # noqa: E402  （名簿 wallets.txt の読み手）
from sources import gmgn  # noqa: E402

JST = 9 * 3600           # 日付の境目は本人の生活時間（JST）で切る。機械の TZ に依存させない
MAX_WALLETS = 10         # 通知に載せる有名ウォレットの行数
TRADERS_LIMIT = 100      # token traders の 1 回の取得件数（API の上限）
RETRY_WAIT = 60          # 取得に失敗したときに 1 回だけ置く間合い（秒）
DEFAULTS = watch.DEFAULTS["hot"]   # 既定値の正本は watch.DEFAULTS（/set・bot のメニューと共有）
PULL_LIMIT = 100         # market trending の 1 回の取得件数（API の上限）
FLAT_X = 1.05            # 直近 24h の最高値÷最安値がこれ未満なら横ばい（勢いなし）
FLAT_MIN_OBS = 6         # 横ばいと言い切るのに要る直近 24h の観測数
FLAT_MIN_SPAN = 12 * 3600  # 同じく、最初の観測からの幅。来たばかりの銘柄は上がった後しか見ていない
GLITCH_RUN = 6           # 「行って戻る」外れ値とみなす最大の連続本数
GLITCH_SPAN = 7200       # 同じく、挟む前後の観測の時刻差の上限。数時間かけた暴落と全戻しは外れ値にしない
GLITCH_LOOKBACK = 7200   # 窓の先頭の外れ値も前後で挟めるよう、24h の手前をこれだけ余分に読む

# 有名ウォレットのタグ → 表示名。無いタグはそのまま出す
TAG_LABELS = {"kol": "KOL", "renowned": "KOL", "smart_degen": "smart"}


# --- 時刻（JST 固定） -------------------------------------------------------

def jst_day(ts):
    """その時刻が属する JST の日付（YYYY-MM-DD）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + JST))


def jst_day_start(ts):
    """その時刻が属する JST 日の 00:00（unix 秒）。"""
    return (int(ts) + JST) // 86400 * 86400 - JST


def fmt_jst(ts, fmt="%Y-%m-%d %H:%M"):
    ts = rules.num(ts)
    if not ts:
        return "?"
    return time.strftime(fmt, time.gmtime(int(ts) + JST))


# --- 設定 -----------------------------------------------------------------

def load_config():
    """watch の設定を読み、config.json に hot ブロックが無ければ既定を書き込む。"""
    cfg = watch.load_config()
    # watch.load_config は 1 階層しかマージしないので、config.json の size / burst が既定の
    # size / burst を丸ごと置き換える。後から足したキー（alt_swaps など）が欠けたままだと
    # 0 扱いで条件が素通りするため、2 階層目も既定で埋める（config.json にある値は上書きしない）
    for part in ("size", "burst"):
        # config.json に "size": null と書かれていても落ちないよう None は空として扱う
        cfg["hot"][part] = {**DEFAULTS[part], **(cfg["hot"].get(part) or {})}
    try:
        with open(watch.CONFIG_PATH, encoding="utf-8") as f:
            stored = json.load(f).get("hot")
    except (OSError, ValueError):
        stored = None
    # キーが増えたときも書き足す（bot のしきい値メニューが実ファイルを見せるため）
    if not isinstance(stored, dict) or set(stored) != set(cfg["hot"]) or any(
            set(stored.get(part) or {}) != set(cfg["hot"][part]) for part in ("size", "burst")):
        watch.save_config({"hot": cfg["hot"]})
    return cfg


# --- 取得 -----------------------------------------------------------------

def _arg(v):
    """gmgn の数値引数。整数値は小数点を付けない（--min-swaps などは int しか受けない）。"""
    return str(int(v)) if float(v).is_integer() else str(v)


def pull_args(chain, pull, limit=PULL_LIMIT, hot=None):
    """trending 24h を 1 とおり取るための gmgn 引数。chg100 は「24h で +100% 以上」だけ。

    floor は judge の規模と勢いの最も緩い下限（代替ルート込み）を満たす銘柄を出来高順に取る。
    swaps / volume の上位 100 件は活発な日に下限より上で切れ、確定しうる中堅が候補に入らなかった
    （2026-10-01 実測: osbook 時価総額 $88 万・保有者 1,844・取引 2,365 がどの並びにも無かった）。
    """
    args = ["market", "trending", "--chain", chain, "--interval", "24h",
            "--limit", str(limit)]
    if pull == "chg100":
        return args + ["--order-by", "volume", "--min-price-change-percent", "100"]
    if pull == "floor":
        hot = hot or DEFAULTS
        size = lambda k: _th(hot, "size", k)    # noqa: E731
        chg = min(_th(hot, "burst", k) for k in ("chg24_pct", "big_chg24_pct",
                                                  "day_range_chg24_pct", "holder_chg24_pct"))
        return args + ["--order-by", "volume",
                       "--min-volume", _arg(min(size("volume_usd"), size("alt_volume_usd"))),
                       "--min-holder-count", _arg(size("holders")),
                       "--min-swaps", _arg(min(size("swaps"), size("alt_swaps"))),
                       "--min-price-change-percent", _arg(chg)]
    return args + ["--order-by", pull]


class Pacer:
    """GMGN 無料枠は分単位の IP 制限なので、連続コールの間に必ず gap 秒空ける。"""

    def __init__(self, gap, sleeper=time.sleep, clock=time.monotonic):
        self.gap = gap
        self.sleeper = sleeper
        self.clock = clock
        self.last = None

    def wait(self):
        if self.last is not None:
            rest = self.gap - (self.clock() - self.last)
            if rest > 0:
                self.sleeper(rest)
        self.last = self.clock()


def fetch(conn, args, pacer, stats):
    """gmgn を 1 回叩く。失敗したら RETRY_WAIT 秒待って 1 回だけやり直す。取れなければ None。

    429 そのものは gm.call（sources.gmgn.call の中身）が待って撃ち直す。ここで見るのは
    それでも駄目だったときの 1 回きりの保険。
    """
    for attempt in (1, 2):
        pacer.wait()
        stats["calls"] += 1
        try:
            data, _retries = gmgn.call(conn, args)
            return data
        except RuntimeError as e:
            stats["errors"] += 1
            print(f"  [hot] 取得失敗（{attempt}/2）: {' '.join(args)}: {e}",
                  file=sys.stderr, flush=True)
            if attempt == 1:
                pacer.sleeper(RETRY_WAIT)
    return None


def fetch_pool(conn, cfg, chains, ts, pacer, stats):
    """各 chain × 各 pull の trending 行を address で union する。

    同じ銘柄が複数の並びに出たら最初の行を採用し、src に取得元のソート名を足す。
    """
    pool = {}
    for chain in chains:
        got = 0
        for pull in cfg["hot"]["pulls"]:
            payload = fetch(conn, pull_args(chain, pull, hot=cfg["hot"]), pacer, stats)
            if payload is None:
                continue
            rows = gmgn.trending_rows(payload)
            got += len(rows)
            if pull == "floor" and len(rows) >= PULL_LIMIT:
                print(f"  [hot] {chain}: floor が上限 {PULL_LIMIT} 件に達した（下限を満たす銘柄を取りこぼしている）",
                      flush=True)
            for r in rows:
                addr = gmgn.norm_addr(chain, r.get("address"))
                if not addr:
                    continue
                key = (chain, addr)
                if key in pool:
                    pool[key]["src"] += "," + pull
                else:
                    pool[key] = obs_row(chain, addr, r, ts, pull)
        if not got:
            print(f"  [hot] {chain}: trending が 1 件も取れなかった（スキップ）", flush=True)
    return pool


# --- 観測 -----------------------------------------------------------------

OBS_COLS = ("chain", "address", "ts", "src", "symbol", "swaps", "buys", "sells",
            "volume", "holders", "price", "market_cap", "liquidity", "chg24",
            "smart_count", "kol_count", "hot_level", "wash", "honeypot",
            "bot_rate", "bundler_rate", "rat_rate", "launchpad", "open_ts",
            "total_supply", "ath_mcap", "twitter")


def _f(v):
    return rules.num(v) or 0.0


def _i(v):
    return int(rules.num(v) or 0)


def obs_row(chain, address, r, ts, src):
    """GMGN の trending 行を hot_obs の 1 行に正規化する。

    price_change_percent は % 単位（100 = +100%）。ここで単位変換はしない。
    """
    return {
        "chain": chain, "address": address, "ts": int(ts), "src": src,
        "symbol": str(r.get("symbol") or ""),
        "swaps": _i(r.get("swaps")), "buys": _i(r.get("buys")), "sells": _i(r.get("sells")),
        "volume": _f(r.get("volume")), "holders": _i(r.get("holder_count")),
        "price": _f(r.get("price")), "market_cap": _f(r.get("market_cap")),
        "liquidity": _f(r.get("liquidity")), "chg24": _f(r.get("price_change_percent")),
        "smart_count": _i(r.get("smart_degen_count")), "kol_count": _i(r.get("renowned_count")),
        "hot_level": _i(r.get("hot_level")),
        "wash": 1 if rules.truthy(r.get("is_wash_trading")) else 0,
        "honeypot": 1 if rules.truthy(r.get("is_honeypot")) else 0,
        "bot_rate": _f(r.get("bot_degen_rate")), "bundler_rate": _f(r.get("bundler_rate")),
        "rat_rate": _f(r.get("rat_trader_amount_rate")),
        "launchpad": str(r.get("launchpad") or ""),
        "open_ts": _i(r.get("open_timestamp")) or _i(r.get("creation_timestamp")),
        "total_supply": _f(r.get("total_supply")), "ath_mcap": _f(r.get("history_highest_market_cap")),
        "twitter": str(r.get("twitter_username") or ""),
    }


def save_obs(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO hot_obs(%s) VALUES(%s)"
        % (",".join(OBS_COLS), ",".join("?" * len(OBS_COLS))),
        [tuple(r.get(c) for c in OBS_COLS) for r in rows])


def day_stats(conn, start, until):
    """その JST 日の自前観測の要約 {(chain,address): {...}}。判定の「今日」はこれだけを使う。

    runup は時刻順に見た「その時点までの安値 → そのときの価格」の最大倍率（正の価格が無ければ None）。
    最高値÷最安値だと高値→安値の暴落も値幅に数えてしまい、day_range で確定した 18 件中 15 件が
    暴落だった（2026-09-20〜24 の hot_obs 再生）。
    peak_mc は [start, until] の market_cap の最大値。
    flat は直近 24h（日の境目ではなく until から 24h）がほぼ横ばいか。judge の勢いを取り消すのに使う。
    """
    out = {}
    for r in conn.execute(
        "SELECT chain,address,COUNT(*) AS n,MAX(price/lo) AS runup,MIN(ts) AS first_ts,"
        " MAX(market_cap) AS peak_mc FROM"
        " (SELECT chain,address,ts,price,market_cap,MIN(NULLIF(price,0)) OVER (PARTITION BY chain,address"
        "   ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS lo"
        "  FROM hot_obs WHERE ts>=? AND ts<=?) GROUP BY chain,address",
            (int(start), int(until))):
        out[(r["chain"], r["address"])] = {"count": r["n"], "runup": r["runup"],
                                           "first_ts": r["first_ts"], "first_holders": None,
                                           "peak_mc": r["peak_mc"], "flat": False}
    for r in conn.execute(
        "SELECT o.chain,o.address,o.holders FROM hot_obs o JOIN"
        " (SELECT chain,address,MIN(ts) AS t FROM hot_obs WHERE ts>=? AND ts<=?"
        "  GROUP BY chain,address) m"
        " ON o.chain=m.chain AND o.address=m.address AND o.ts=m.t",
            (int(start), int(until))):
        d = out.get((r["chain"], r["address"]))
        if d is not None:
            d["first_holders"] = r["holders"]
    series = {}
    for r in conn.execute(
        "SELECT chain,address,ts,price FROM hot_obs WHERE ts>? AND ts<=? ORDER BY chain,address,ts",
            (int(until) - 86400 - GLITCH_LOOKBACK, int(until))):
        series.setdefault((r["chain"], r["address"]), []).append((r["ts"], _f(r["price"])))
    for key, s in series.items():
        d = out.get(key)
        if d is None:
            continue
        bad = _glitch_lows(s)
        win = [(i, t, p) for i, (t, p) in enumerate(s) if t > int(until) - 86400]
        good = [p for i, _t, p in win if p > 0 and i not in bad]
        d["flat"] = (len(good) >= FLAT_MIN_OBS and int(until) - win[0][1] >= FLAT_MIN_SPAN
                     and max(good) / min(good) < FLAT_X)
    return out


def _glitch_lows(s):
    """「行って戻る」一時的な安値の添字の集合。s は時刻順の (ts, price)。

    前後の観測が ±10% で一致し、間の最大 GLITCH_RUN 本がその低い方の 0.6 倍未満で、前後の時刻差が
    GLITCH_SPAN 以内なら外れ値とみなす。観測が飛んだ本物の V 字（-22h→-8h の間に暴落）は除かない。
    GMGN が一瞬だけ誤った価格を返す（DAI 09-28 07:11〜07:32 に 0.50→0.50→0.12 ドルと出て、次で 1.00 ドルに戻った）。
    上に跳ねる外れ値は残す（横ばいと言えなくなるだけで、確定を取り消す向きには働かない）。
    """
    bad = set()
    n = len(s)
    ps = [p for _, p in s]
    for i in range(1, n - 1):
        for j in range(i, min(i + GLITCH_RUN, n - 1)):
            a, b = ps[i - 1], ps[j + 1]
            if a > 0 and b > 0 and abs(b / a - 1) <= 0.10 and max(ps[i:j + 1]) < 0.6 * min(a, b) \
                    and s[j + 1][0] - s[i - 1][0] <= GLITCH_SPAN:
                bad.update(range(i, j + 1))
    return bad


def latest_obs(conn, start, until, chains=()):
    """その JST 日の銘柄ごとの最新観測（惜しい・継続の判定に使う）。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT o.* FROM hot_obs o JOIN"
        " (SELECT chain,address,MAX(ts) AS t FROM hot_obs WHERE ts>=? AND ts<=?"
        "  GROUP BY chain,address) m"
        " ON o.chain=m.chain AND o.address=m.address AND o.ts=m.t",
        (int(start), int(until)))]
    if chains:
        rows = [r for r in rows if r["chain"] in set(chains)]
    return rows


def prune(conn, now, days):
    conn.execute("DELETE FROM hot_obs WHERE ts < ?", (int(now - days * 86400),))


# --- 判定 -----------------------------------------------------------------

SIZE_PARTS = (("swaps", "swaps", "取引"), ("volume", "volume_usd", "出来高"),
              ("holders", "holders", "保有者"))


def _miss_label(key, label, v):
    """足りなかった規模の項目を「保有者 280」の形で（出来高だけ金額表記）。"""
    return f"{label} {rules.fmt_usd(v)}" if key == "volume" else f"{label} {int(v):,}"


def _th(hot, part, key):
    """しきい値 hot[part][key]。キーが無ければ 0 ではなく既定値（watch.DEFAULTS["hot"]）を使う。

    config.json の size / burst は既定を丸ごと置き換えるので、後から足したキーが欠けうる。
    0 扱いにすると例えば alt_holders=0・alt_swaps=0 で取引数の条件が実質無効になる。
    """
    v = (hot.get(part) or {}).get(key)
    return _f(DEFAULTS[part][key] if v is None else v)


def _holders_grew(holders, first, chg24, hot):
    """保有者の増加で勢いありとみなすか。人数・伸び率・24h 変化率の 3 つ全部が要る。

    人数（+300）だけだと保有者数万の既存銘柄・株式トークン（QQQB, NVDAB, ZEC など）が価格横ばいでも通る。
    09-20〜24 の再生で、旧判定で保有者増から確定した 36 件のうち 28 件は 24h +20% 未満（横ばいの大型・株式トークン）。
    残る 8 件も 1 件（旺柴 +20.9%）を除き保有者の伸びは 1〜7%。時価総額 $2M〜$130M の既存銘柄で、確定後 2 時間の高値は
    +0〜31% だった（2026-09-20〜24 の再生）。
    """
    g = holders - first
    return (g >= _th(hot, "burst", "holder_growth")
            and g >= first * _th(hot, "burst", "holder_growth_pct") / 100
            and chg24 >= _th(hot, "burst", "holder_chg24_pct"))


def judge(row, hot, day=None):
    """1 銘柄の判定（純関数）。row は obs_row、day は day_stats のその銘柄ぶん。

    戻りは {"ok","size","burst","quality","burst_why","size_miss","peak_mc","flat"}。
    ok = 規模も勢いも品質も満たした。倍率は使わない。ピーク時価総額は size.peak_mcap_usd の
    下限と、出来高の代替（size.alt_*）にだけ使う。
    """
    swaps, holders = _f(row.get("swaps")), _f(row.get("holders"))
    volume = _f(row.get("volume"))
    peak = max(_f(day.get("peak_mc")) if day else 0.0, _f(row.get("market_cap")))
    # 取引数は「保有者 alt_holders 以上かつ取引 alt_swaps 以上」でも足りる。保有者 1,000〜14,000 人・
    # 24h +150%〜+7000% なのに取引 2,000〜2,900 回で落ちていた（派人生・宇宙所・AGI・SafeX）
    alt_swaps = holders >= _th(hot, "size", "alt_holders") and \
        swaps >= _th(hot, "size", "alt_swaps")
    # 出来高が薄くても、alt_swaps の前提（保有者・取引が多い）に加えピークも大きければ足りる。
    # 09-20〜24 の再生で DCA・美股人生・八仙・币安天使と玉兔の 09-23 の再上昇を拾い、币安协议・蝴蝶家园は
    # 1 日早く確定する（どれも出来高 $15〜22 万・ピーク $30〜88 万・保有者 1,100〜4,100 人）
    alt_volume = alt_swaps and peak >= _th(hot, "size", "alt_peak_mcap_usd") \
        and volume >= _th(hot, "size", "alt_volume_usd")
    miss = [_miss_label(key, label, _f(row.get(key)))
            for key, cfg_key, label in SIZE_PARTS
            if _f(row.get(key)) < _th(hot, "size", cfg_key)
            and not (key == "swaps" and alt_swaps)
            and not (key == "volume" and alt_volume)]
    # その日のピークが小さい銘柄は確定しない（2026-09-25 本人決定）。09-20〜24 の再生で 53 件が外れ、
    # 19 件は出来高がピークの 10 倍以上ある回転売買だった（MEMESTOCKS: ピーク $51.2K・出来高 $2.3M）
    if peak < _th(hot, "size", "peak_mcap_usd"):
        miss.append(f"ピーク時価総額 {rules.fmt_usd(peak)}")
    chg24 = _f(row.get("chg24"))
    why = ""
    if chg24 >= _th(hot, "burst", "chg24_pct"):
        why = "chg24"
    elif swaps >= _th(hot, "burst", "big_swaps") and chg24 >= _th(hot, "burst", "big_chg24_pct"):
        why = "big_swaps"
    # 値幅は安値→その後の高値（runup）。高値→安値の暴落は入らない（day_stats の説明を参照）
    elif day and (day.get("count") or 0) >= 2 and \
            _f(day.get("runup")) >= _th(hot, "burst", "day_range_x") and \
            chg24 >= _th(hot, "burst", "day_range_chg24_pct"):
        why = "day_range"
    elif day and day.get("first_holders") is not None and \
            _holders_grew(holders, _f(day.get("first_holders")), chg24, hot):
        why = "holders"
    # 直近 24h の自前観測がほぼ横ばいなら、GMGN の 24h 変化率や外れ値からの値幅だけで勢いとしない。
    # ステーブルコインの DAI が一瞬の 0.12 ドル表示を安値に値幅 8 倍で確定していた（09-28）。
    # 09-20〜10-01 の再生で外れるのは DAI の 1 件だけ（ほかの 264 件は確定も時刻も変わらない）
    flat = bool(day and day.get("flat"))
    if flat:
        why = ""
    quality = not row.get("wash") and not row.get("honeypot")
    size = not miss
    burst = bool(why)
    return {"ok": size and burst and quality, "size": size, "burst": burst,
            "quality": quality, "burst_why": why, "size_miss": miss, "peak_mc": peak,
            "flat": flat}


# --- 連続通過と確定 ---------------------------------------------------------

def bump(conn, chain, address, ok, now):
    """判定結果で連続通過数を進める（不通過は 0 に戻す）。戻りは新しい streak。"""
    r = conn.execute("SELECT streak FROM hot_state WHERE chain=? AND address=?",
                     (chain, address)).fetchone()
    streak = ((r["streak"] or 0) if r else 0) + 1 if ok else 0
    conn.execute(
        "INSERT INTO hot_state(chain,address,streak,last_pass_ts) VALUES(?,?,?,?)"
        " ON CONFLICT(chain,address) DO UPDATE SET streak=excluded.streak,"
        " last_pass_ts=COALESCE(excluded.last_pass_ts, hot_state.last_pass_ts)",
        (chain, address, streak, int(now) if ok else None))
    return streak


def reset_missing(conn, chains, seen):
    """この周期のプールに居なかった銘柄は連続が途切れたものとして 0 に戻す。"""
    for chain in chains:
        gone = [r["address"] for r in conn.execute(
            "SELECT address FROM hot_state WHERE chain=? AND streak>0", (chain,))
            if (chain, r["address"]) not in seen]
        for addr in gone:
            conn.execute("UPDATE hot_state SET streak=0 WHERE chain=? AND address=?",
                         (chain, addr))


def confirmable(conn, chain, address, streak, hot, now):
    """連続通過が足りていて、前の通知から cooldown_hours 以上空いているか。"""
    if streak < int(hot["confirm_passes"]):
        return False
    r = conn.execute("SELECT last_notified_ts FROM hot_state WHERE chain=? AND address=?",
                     (chain, address)).fetchone()
    last = rules.num(r["last_notified_ts"]) if r else None
    return not last or (now - last) >= float(hot["cooldown_hours"]) * 3600


def open_hit(conn, row, now):
    """確定を hot_hits に 1 行。同じ JST 日に同じ銘柄が既にあれば False（通知しない）。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO hot_hits(chain,address,day_jst,symbol,confirmed_ts,sent)"
        " VALUES(?,?,?,?,?,0)",
        (row["chain"], row["address"], jst_day(now), row["symbol"], int(now)))
    return cur.rowcount > 0


def close_hit(conn, row, now, message, wallets, sent):
    conn.execute(
        "UPDATE hot_hits SET notified_ts=?,sent=?,message=?,row_json=?,wallets_json=?"
        " WHERE chain=? AND address=? AND day_jst=?",
        (int(now), 1 if sent else 0, message, json.dumps(row, ensure_ascii=False),
         json.dumps(wallets, ensure_ascii=False), row["chain"], row["address"], jst_day(now)))
    conn.execute("UPDATE hot_state SET last_notified_ts=? WHERE chain=? AND address=?",
                 (int(now), row["chain"], row["address"]))


# --- 確定銘柄の追加取得 ------------------------------------------------------

def traders_rows(payload):
    """token traders の行。{"list": [...]} も {"data": {"list": [...]}} も受ける。空は空。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    lst = payload.get("list") if isinstance(payload, dict) else payload
    if not isinstance(lst, list):
        return []
    return [r for r in lst if isinstance(r, dict) and r.get("address")]


def kline_rows(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    lst = payload.get("list") if isinstance(payload, dict) else payload
    if not isinstance(lst, list):
        return []
    return [r for r in lst if isinstance(r, dict)]


def fetch_wallets(conn, chain, address, pacer, stats):
    """買い手上位 ＋ smart_degen ＋ renowned の 3 本を address で union した生の行。"""
    calls = [["token", "traders", "--chain", chain, "--address", address,
              "--limit", str(TRADERS_LIMIT), "--order-by", "buy_volume_cur"]]
    for tag in ("smart_degen", "renowned"):
        calls.append(["token", "traders", "--chain", chain, "--address", address,
                      "--limit", str(TRADERS_LIMIT), "--tag", tag])
    rows, seen = [], set()
    for args in calls:
        payload = fetch(conn, args, pacer, stats)
        if payload is None:
            continue
        for r in traders_rows(payload):
            key = str(r.get("address") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                rows.append(r)
    return rows


def fetch_kline(conn, chain, address, start, now, pacer, stats):
    """今日（JST 00:00 以降）の 1 時間足。取れなければ None（通知は出す）。"""
    payload = fetch(conn, ["market", "kline", "--chain", chain, "--address", address,
                           "--resolution", "1h", "--from", str(int(start)),
                           "--to", str(int(now))], pacer, stats)
    if payload is None:
        return None
    rows = kline_rows(payload)
    return rows or None


def kline_day(rows, total_supply):
    """今日の安値・高値と「高値時の時価総額」。数えられなければ None。"""
    if not rows:
        return None
    lows = [_f(r.get("low")) for r in rows if _f(r.get("low")) > 0]
    highs = [_f(r.get("high")) for r in rows if _f(r.get("high")) > 0]
    if not lows or not highs:
        return None
    lo, hi = min(lows), max(highs)
    supply = _f(total_supply)
    return {"low": lo, "high": hi, "x": hi / lo, "mcap": hi * supply if supply > 0 else None}


def famous_wallets(rows, listed, hot):
    """有名ウォレット。タグ一致か名簿一致で拾い、bot 判定のタグは落とす。

    並びは 名簿 → buy_volume_cur 降順。最大 MAX_WALLETS 行。
    """
    want = {str(t).lower() for t in (hot.get("wallet_tags") or ())}
    bad = {str(t).lower() for t in (hot.get("wallet_exclude_tags") or ())}
    out = []
    for r in rows:
        addr = str(r.get("address") or "").strip()
        key = addr.lower()
        if not key:
            continue
        tags = {str(t).strip().lower() for t in (r.get("tags") or []) if t}
        if tags & bad:
            continue
        entry = listed.get(key)
        hit = tags & want
        if not entry and not hit:
            continue
        name = (entry or {}).get("name") or r.get("name") or r.get("twitter_name") or addr[:10]
        handle = (entry or {}).get("handle") or r.get("twitter_username") or ""
        if entry:
            kind = "名簿"
        else:
            tag = next((t for t in (hot.get("wallet_tags") or ()) if t.lower() in hit), "")
            kind = TAG_LABELS.get(tag.lower(), tag)
        out.append({"address": addr, "name": str(name), "twitter": str(handle),
                    "kind": kind, "listed": bool(entry),
                    "buy": _f(r.get("buy_volume_cur")), "profit": _f(r.get("realized_profit"))})
    out.sort(key=lambda w: (0 if w["listed"] else 1, -w["buy"]))
    return out[:MAX_WALLETS]


# --- 本文 -----------------------------------------------------------------

def fmt_signed(v):
    v = rules.num(v) or 0.0
    return ("+" if v >= 0 else "−") + rules.fmt_usd(abs(v))


def fmt_rate(v):
    """0〜1 の比率を % の整数で（bot 比率などの注意行）。"""
    return f"{round(_f(v) * 100)}%"


def wallet_line(w):
    bits = [watch.sanitize(w["name"], 24) or w["address"][:10]]
    if w.get("twitter"):
        bits.append("@" + watch.sanitize(w["twitter"], 20))
    bits.append(f"[{w['kind']}]")
    return f"- {' '.join(bits)} 買 {rules.fmt_usd(w['buy'])} / 損益 {fmt_signed(w['profit'])}"


def build_message(row, wallets, day, kline_failed=False):
    """確定 1 件の通知本文。Markdown もボタンも点の内訳も付けない。"""
    sym = watch.sanitize(row.get("symbol")) or "?"
    lines = [f"【盛り上がり】{sym}（{row['chain'].upper()}）",
             f"24h: 出来高 {rules.fmt_usd(row['volume'])} / 取引 {_i(row['swaps']):,} 回"
             f"（買 {_i(row['buys']):,}・売 {_i(row['sells']):,}） / 保有者 {_i(row['holders']):,}"]
    chg = _f(row.get("chg24"))
    price = f"価格 24h {rules.fmt_pct(chg)}"
    if chg > 0:
        price += f"（{rules.fmt_x(1 + chg / 100)}）"
    lines.append(price)
    if kline_failed or not day:
        lines.append("今日: 取得失敗")
    else:
        today = f"今日: 安値→高値 {rules.fmt_x(day['x'])}"
        if day.get("mcap"):
            today += f"・高値時の時価総額 {rules.fmt_usd(day['mcap'])}"
        lines.append(today)
    mcap = f"時価総額 {rules.fmt_usd(row['market_cap'])}"
    if _f(row.get("ath_mcap")) > 0:
        mcap += f"（史上最高 {rules.fmt_usd(row['ath_mcap'])}）"
    lines.append(mcap)
    head = []
    if _i(row.get("open_ts")) > 0:
        lp = watch.sanitize(row.get("launchpad"), 16)
        head.append(f"上場 {fmt_jst(row['open_ts'])} JST" + (f"（{lp}）" if lp else ""))
    head.append(f"流動性 {rules.fmt_usd(row['liquidity'])}")
    lines.append("・".join(head))

    lines.append("")
    if wallets:
        lines.append("有名ウォレット（買い手上位 100 ＋ タグ検索から）:")
        lines += [wallet_line(w) for w in wallets]
    else:
        lines.append("有名ウォレット: 該当なし")

    lines.append("")
    rates = [_f(row.get(k)) for k in ("bot_rate", "bundler_rate", "rat_rate")]
    if any(rates):
        lines.append(f"注意: bot 比率 {fmt_rate(rates[0])}・バンドル {fmt_rate(rates[1])}"
                     f"・ネズミ {fmt_rate(rates[2])}")
    lines.append(watch.gmgn_url(row["chain"], row["address"]))
    return "\n".join(lines)


def telegram_sender():
    """本文 1 本を送って真偽を返す関数。資格情報が無ければ常に False。"""
    token, chat_id = notify.credentials()

    def send(text):
        return bool(token and chat_id and notify.send(text, token, chat_id))
    return send


# --- 日次の集計 -------------------------------------------------------------

def daily_counts(conn, now):
    raw = db.kv_get(conn, "hot.daily")
    try:
        cur = json.loads(raw) if raw else None
    except ValueError:
        cur = None
    if not isinstance(cur, dict) or cur.get("day") != jst_day(now):
        cur = {"day": jst_day(now), "cycles": 0, "calls": 0, "errors": 0}
    return cur


def bump_daily(conn, now, stats):
    cur = daily_counts(conn, now)
    cur["cycles"] += 1
    cur["calls"] += stats.get("calls", 0)
    cur["errors"] += stats.get("errors", 0)
    db.kv_set(conn, "hot.daily", json.dumps(cur))
    return cur


def lane_line(conn, now=None):
    """/status に出す 1 行。"""
    now = int(time.time() if now is None else now)
    ts = rules.num(db.kv_get(conn, "hot.last_cycle_ts"))
    hits = conn.execute("SELECT COUNT(*) AS n FROM hot_hits WHERE day_jst=?",
                        (jst_day(now),)).fetchone()["n"]
    when = "なし" if not ts else f"{fmt_jst(ts, '%H:%M')}（{int((now - ts) // 60)} 分前）"
    return f"盛り上がりレーン: 最終巡回 {when}・今日の確定 {hits} 件"


# --- 巡回 -----------------------------------------------------------------

def cycle(conn, cfg, now=None, sleeper=time.sleep, sender=None, echo=print):
    """取得 → 観測を保存 → 判定 → 連続通過 → 確定したら追加取得して 1 通。"""
    hot = cfg["hot"]
    now = int(time.time() if now is None else now)
    chains = list(cfg.get("chains") or ())
    stats = {"calls": 0, "errors": 0}
    pacer = Pacer(_f(hot["api_gap_sec"]), sleeper=sleeper)
    pool = fetch_pool(conn, cfg, chains, now, pacer, stats)
    if pool:
        save_obs(conn, list(pool.values()))

    start = jst_day_start(now)
    stats_day = day_stats(conn, start, now)
    hits, near = [], 0
    for key, row in sorted(pool.items(), key=lambda kv: -_f(kv[1]["volume"])):
        v = judge(row, hot, stats_day.get(key))
        streak = bump(conn, row["chain"], row["address"], v["ok"], now)
        if not v["ok"]:
            if v["size"] != v["burst"] and not v["flat"]:   # 横ばいで勢いを消した銘柄は惜しくない
                near += 1
            continue
        if confirmable(conn, row["chain"], row["address"], streak, hot, now):
            hits.append(row)
    reset_missing(conn, chains, set(pool))

    listed = load_wallets(cfg)
    send = sender if sender is not None else (
        None if cfg.get("dry_run", True) or not hits else telegram_sender())
    sent_n = 0
    for row in hits:
        if not open_hit(conn, row, now):
            continue                       # 同じ JST 日に確定済み（日次まとめには載る）
        wallets = famous_wallets(fetch_wallets(conn, row["chain"], row["address"], pacer, stats),
                                 listed, hot)
        kline = fetch_kline(conn, row["chain"], row["address"], start, now, pacer, stats)
        text = build_message(row, wallets, kline_day(kline, row["total_supply"]),
                             kline_failed=kline is None)
        ok = bool(send(text)) if send else False
        close_hit(conn, row, now, text, wallets, ok)
        sent_n += 1 if ok else 0
        echo(text if not ok else f"[{fmt_jst(now)}] hot sent {row['symbol']} ({len(text)} 文字)")

    prune(conn, now, _f(hot["retention_days"]))
    db.kv_set(conn, "hot.last_cycle_ts", now)
    daily = bump_daily(conn, now, stats)
    conn.commit()
    echo(f"[{fmt_jst(now)}] hot pool={len(pool)} hit={len(hits)} near={near} "
         f"sent={sent_n} calls={stats['calls']} errors={stats['errors']} "
         f"cycles={daily['cycles']}")
    return {"pool": len(pool), "hit": len(hits), "near": near, "sent": sent_n, **stats}


# --- 日次まとめ ------------------------------------------------------------

def _sum_line(row):
    return (f"出来高 {rules.fmt_usd(row.get('volume'))} / 取引 {_i(row.get('swaps')):,}"
            f" / 保有者 {_i(row.get('holders')):,} / 24h {rules.fmt_pct(_f(row.get('chg24')))}")


def digest_since(conn, now):
    """まとめの期間の起点＝前回まとめを送った時刻（kv hot.last_digest_ts）。

    無い・数値でない・now 以上なら 24 時間前。`since >= now` ではなく `not since < now` で
    見ているのは NaN（"nan" は float になる）も弾くため。
    """
    since = rules.num(db.kv_get(conn, "hot.last_digest_ts"))
    if since is None or not since < now:
        return int(now) - 86400
    return int(since)


def _in_flight(h, done_ts):
    """処理中の確定か。notified_ts が NULL で、作った巡回がまだ終わっていない（done_ts は kv hot.last_cycle_ts）。

    done_ts が無い（初回・作り直し直後）なら NULL の行は全部処理中。`>` でなく `not <=` なのは NaN も処理中に倒すため。
    """
    return h["notified_ts"] is None and (done_ts is None or not _f(h["confirmed_ts"]) <= done_ts)


def digest_hits(conn, now):
    """まとめに載せる確定行を hot_hits.id で拾う（戻りは (hits, last_id)）。

    巡回（cmd_cycle）は自動コミットなので、open_hit の行は INSERT した時点でコミットされ、GMGN を取る
    約 50 秒のあいだ row_json・notified_ts が NULL のまま見える（処理中の行）。id は SQLite の書き込み
    ロックの下で採番されるので、コミット順＝id 順になる。前回までに載せた最大 id（kv hot.last_digest_id）
    より後を拾えば、confirmed_ts（巡回開始時刻）に関係なく取りこぼさない。

    処理中の行があれば、その最小 id（cut）から後は今回載せず last_id を cut - 1 で止める（中身が空のまま
    載せてカーソルが越えると、その確定は二度と中身付きで載らない）。notified_ts が NULL でも、それを作った
    巡回が終わっている行（巡回が open_hit と close_hit の間で落ちた残骸）は載せて越える。
    kv が無い・数値でない・MAX(id) より大きい（hot_hits を作り直した等）ときは直近 24 時間を拾う。
    last_id は確定 0 件でも MAX(id) を返す（起点を保存して次回から id で切る。24 時間で拾う回が続くと、
    起動が遅れた日に取りこぼす）。
    """
    # 読む順は ① hot.last_cycle_ts → ② hot.last_digest_id → ③ MAX(id) → ④ 行。
    # ① を ④ より先に読むのは、後に読むと、行を読んだ直後に close_hit して終わった巡回の行を「巡回は
    # 終わったのに notified_ts が NULL＝残骸」と取り違え、中身が空のまま完了扱いにしてしまうため。
    # ③ も ④ より先に読む（③ の後に書かれた行は ④ で読めるか last_id より後に残るかで、読まずに越えない）
    done_ts = rules.num(db.kv_get(conn, "hot.last_cycle_ts"))
    after = rules.num(db.kv_get(conn, "hot.last_digest_id"))
    top = conn.execute("SELECT MAX(id) AS m FROM hot_hits").fetchone()["m"]
    if after is not None and math.isfinite(after) and top is not None and after > top:
        print(f"  [hot] hot.last_digest_id={after:g} が hot_hits の最大 id {top} より大きい"
              "（作り直した？）。直近 24 時間で拾う", file=sys.stderr, flush=True)
        after = None
    if after is None or not math.isfinite(after):
        # 上限（confirmed_ts <= now）は付けない。読んだ時点でコミット済みの行を落とすと、カーソルが越えて永久に載らない
        rows = conn.execute("SELECT * FROM hot_hits WHERE confirmed_ts>?"
                            " ORDER BY confirmed_ts, id", (int(now) - 86400,))
    else:
        rows = conn.execute("SELECT * FROM hot_hits WHERE id>? ORDER BY confirmed_ts, id",
                            (int(after),))
    hits = [dict(r) for r in rows]
    busy = [h["id"] for h in hits if _in_flight(h, done_ts)]
    if busy:
        cut = min(busy)
        return [h for h in hits if h["id"] < cut], cut - 1
    ids = [i for i in [top] + [h["id"] for h in hits] if i is not None]
    return hits, (int(max(ids)) if ids else None)


def _near_rank(row, v, hot):
    """惜しいの近さ（しきい値に対する達成度。大きいほど惜しい）。

    出来高順だと 24h が -9〜+8% の大型銘柄（QQQB・ZEC 等）が上位を埋めて役に立たなかった。
    勢い不足は 24h 変化率 ÷ burst.chg24_pct。規模不足は保有者・ピーク・「取引と出来高」の達成度の最小で、
    取引と出来高は judge の 3 ルート（通常／取引の代替／取引と出来高の代替）ごとに小さい方を取った最大。
    代替で足りている項目を不足と数えると、出来高だけ少し足りない銘柄を全項目少しずつ足りない銘柄より下に並べていた。
    項目ごとに大きい方を取るだけだと、代替のしきい値を通常より厳しくしたとき（例 alt_swaps 4000）に
    規模不足の行が達成度 1 以上になる（2026-09-25 レビュー）。
    """
    def rate(key, part, th_key):
        return _f(row.get(key)) / (_th(hot, part, th_key) or 1)   # しきい値 0 でも 0 除算しない
    if v["size"]:
        return rate("chg24", "burst", "chg24_pct")
    peak = _f(v.get("peak_mc")) if v.get("peak_mc") is not None else _f(row.get("market_cap"))
    routes = [(rate("swaps", "size", "swaps"), rate("volume", "size", "volume_usd"))]
    if _f(row.get("holders")) >= _th(hot, "size", "alt_holders"):
        alt_swaps = rate("swaps", "size", "alt_swaps")
        routes.append((alt_swaps, rate("volume", "size", "volume_usd")))
        if peak >= _th(hot, "size", "alt_peak_mcap_usd"):
            routes.append((alt_swaps, rate("volume", "size", "alt_volume_usd")))
    return min(max(min(r) for r in routes), rate("holders", "size", "holders"),
               peak / (_th(hot, "size", "peak_mcap_usd") or 1))


def digest_text(conn, cfg, now, hits=None):
    """確定の一覧と、今日（JST）の継続・惜しいを 1 通に。確定 0 件でもそのまま送る。

    hits を渡すのは日次まとめ（cmd_digest が digest_hits で拾った「前回まとめ以降」の確定）。以前は day_jst で
    切って 23:00 に送っていたため、まとめの後（23 時台）に確定した分がどのまとめにも載らなかった（09-20〜23 で 17 件）。
    hits が None（/hot）は今日（JST）の確定を出す。まとめの起点で切ると 23:00 のまとめ後に /hot を開いたとき
    今日の確定が消えていたため。確定はチェーンで絞らない（/chains で外したチェーンの確定が表示されないまま
    カーソルだけ越えて永久に消えるため）。今の chains に無いチェーンの行だけ [ARC] のように印を付ける。
    ここは読むだけで kv を進めない（進めるのは送信に成功した cmd_digest だけ）。
    """
    hot = cfg["hot"]
    now = int(now)
    day, start = jst_day(now), jst_day_start(now)
    chains = list(cfg.get("chains") or ())
    label = "・".join(c.upper() for c in chains) or "?"
    # 本文の無い行が処理中か残骸かを分けるのに使う。/hot では行より先に読む（digest_hits と同じ理由。後に読むと、
    # 行を読んだ直後に終わった巡回の行を「未完了」と取り違える）
    done_ts = rules.num(db.kv_get(conn, "hot.last_cycle_ts"))

    if hits is None:
        hits = [dict(r) for r in conn.execute(
            "SELECT * FROM hot_hits WHERE day_jst=? ORDER BY confirmed_ts, id", (day,))]
        lines = [f"【今日の盛り上がり】{day}（{label}）確定 {len(hits)} 件"]
    else:
        since = digest_since(conn, now)
        lines = [f"【今日の盛り上がり】{day}（{label}）確定 {len(hits)} 件"
                 f"（{fmt_jst(since, '%m-%d %H:%M')} 以降）"]
    # 継続から外すのは今までどおり今日（JST）の確定。加えて上に載せた前日 23 時台の確定も二重に出さない
    done = {(r["chain"], r["address"]) for r in conn.execute(
        "SELECT chain,address FROM hot_hits WHERE day_jst=?", (day,))}
    for i, h in enumerate(hits, 1):
        done.add((h["chain"], h["address"]))
        try:
            row = json.loads(h["row_json"] or "{}")
        except ValueError:
            row = {}
        if not row:
            # 本文の無い行（処理中・残骸）は、同じ巡回が open_hit の前に保存した観測（ts=confirmed_ts）の数値を出す
            r = conn.execute("SELECT * FROM hot_obs WHERE chain=? AND address=? AND ts=?",
                             (h["chain"], h["address"], h["confirmed_ts"])).fetchone()
            if r:
                row = dict(r)
        wallets = []
        try:
            wallets = json.loads(h["wallets_json"] or "[]")
        except ValueError:
            pass
        sym = watch.sanitize(h["symbol"] or row.get("symbol")) or "?"
        tag = "" if h["chain"] in chains else f"[{h['chain'].upper()}] "   # 今は巡回していないチェーン
        # notified_ts が NULL は巡回が open_hit と close_hit の間（処理中か、落ちた残骸）で本文が無い。
        # 処理中は作った巡回が GMGN を取っている最中で、/hot でだけ見える（まとめは digest_hits が次回に回す）。
        # 残骸と同じ「未完了」にすると落ちたように見えるので分ける。
        # sent=0 は dry_run 中の確定（本文はログにだけ出た）。時刻を出すと送ったように見えるので区別する
        if h["notified_ts"] is None:
            when = "処理中" if _in_flight(h, done_ts) else "未完了"
        elif h["sent"]:
            when = fmt_jst(h["notified_ts"], "%H:%M")
        else:
            when = "未送信"
        lines.append(f"{i}. {tag}{sym} {_sum_line(row)} / 有名 {len(wallets)} / 通知 {when}")

    latest = latest_obs(conn, start, now, chains)
    stats_day = day_stats(conn, start, now)
    cont, near = [], []
    for row in latest:
        key = (row["chain"], row["address"])
        v = judge(row, hot, stats_day.get(key))
        if v["ok"]:
            if key in done:
                continue
            st = conn.execute("SELECT streak FROM hot_state WHERE chain=? AND address=?",
                              key).fetchone()
            if st and (st["streak"] or 0) >= int(hot["confirm_passes"]):
                cont.append(row)
            continue
        if v["size"] != v["burst"] and not v["flat"]:
            near.append((row, v))
    for row in sorted(cont, key=lambda r: -_f(r["volume"])):
        sym = watch.sanitize(row.get("symbol")) or "?"
        lines.append(f"（継続）{sym} {_sum_line(row)} / 前日確定・再通知なし")

    lines += ["", "惜しい（規模か勢いの片方だけ）:"]
    near.sort(key=lambda t: (-_near_rank(t[0], t[1], hot), -_f(t[0]["volume"])))
    for row, v in near[:int(hot["near_limit"])]:
        sym = watch.sanitize(row.get("symbol")) or "?"
        why = "勢い不足" if v["size"] else f"規模不足（{'・'.join(v['size_miss'])}）"
        lines.append(f"- {sym} {_sum_line(row)} → {why}")
    if not near:
        lines.append("- なし")

    d = daily_counts(conn, now)
    lines += ["", f"巡回 {d['cycles']} 回・GMGN 呼び出し {d['calls']} 回・エラー {d['errors']} 回"]
    return "\n".join(lines)


# --- サブコマンド ----------------------------------------------------------

def cmd_cycle(args):
    conn = db.connect()
    # 自動コミット。API を待つあいだ書き込みロックを握らない（3 プロセスで共有するため）
    conn.isolation_level = None
    cfg = load_config()
    cycle(conn, cfg)
    conn.close()


def cmd_digest(args):
    conn = db.connect()
    # 本番の巡回・bot は自動コミットで書き込みロックを長く握らない。それでも sqlite3 シェルの開きっぱなしなど
    # 外部の長い書き込みがあると、既定 30 秒で諦めて送信後に起点を書けず、翌日のまとめに同じ確定が二重に載る
    conn.execute("PRAGMA busy_timeout=1800000")
    cfg = load_config()
    now = int(time.time())
    # 本文と起点（last_id）を同じ読み取り結果から決める。別々に読むと、その間に書かれた確定を飛ばす
    hits, last_id = digest_hits(conn, now)
    text = digest_text(conn, cfg, now, hits)
    token, chat_id = notify.credentials()
    # まとめはドライラン中でも送る（1 日 1 通なので通知過多にならない）
    if token and chat_id and notify.send(text, token, chat_id):
        # 次のまとめの起点。送れたときだけ進める（送れなければ次のまとめに同じ確定をもう一度載せる）
        db.kv_set(conn, "hot.last_digest_ts", now)
        if last_id is not None:
            db.kv_set(conn, "hot.last_digest_id", last_id)
        conn.commit()
        print(f"[{fmt_jst(now)}] hot digest sent ({len(text)} 文字)", flush=True)
    else:
        # launchd のログで失敗が目立つよう、本文の前に 1 行出す（認証情報が無いときも同じ）
        print(f"[{fmt_jst(now)}] hot digest NOT sent（起点は進めない。次のまとめに同じ確定をもう一度載せる）",
              flush=True)
        print(text)
    conn.close()


def cmd_judge(args):
    """DB に溜まっている最新観測を判定して表に出す（API は叩かない）。"""
    conn = db.connect()
    cfg = load_config()
    now = int(time.time())
    start = jst_day_start(now)
    stats_day = day_stats(conn, start, now)
    rows = latest_obs(conn, start, now, cfg.get("chains") or ())
    rows.sort(key=lambda r: -_f(r["volume"]))
    hit = 0
    for row in rows:
        v = judge(row, cfg["hot"], stats_day.get((row["chain"], row["address"])))
        tag = "HIT " if v["ok"] else ("MISS" if v["quality"] else "BAD ")
        if v["ok"]:
            hit += 1
        st = conn.execute("SELECT streak FROM hot_state WHERE chain=? AND address=?",
                          (row["chain"], row["address"])).fetchone()
        print(f"{tag} {(row['symbol'] or '?')[:12]:>12} {_sum_line(row)}"
              f" streak={(st['streak'] if st else 0) or 0}"
              f" size={'o' if v['size'] else 'x'} burst={v['burst_why'] or 'x'}"
              f" src={row['src']}")
    print(f"pool={len(rows)} hit={hit}（{jst_day(now)} の最新観測。API は叩いていない）")
    conn.close()


def main():
    p = argparse.ArgumentParser(description="すでに盛り上がった銘柄を確定して通知する")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cycle", help="10 分ごとの巡回（launchd 用）").set_defaults(fn=cmd_cycle)
    sub.add_parser("digest", help="日次まとめを Telegram に送る").set_defaults(fn=cmd_digest)
    sub.add_parser("judge", help="溜めた観測を判定するだけ（API を叩かない）").set_defaults(fn=cmd_judge)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
