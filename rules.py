"""S1〜S6（急増）と M1〜M4（マイルストーン）の発火判定。

DB にも API にも触らない純粋関数なので合成データでテストできる。

evaluate() の入力:
  token   : tokens テーブルの行（first_price / first_mcap / creation_ts / first_seen_ts）
  history : db.get_history() の戻り（fired / recent_triggers / snapshots）※今回の行を入れる前
  row     : 今回の正規化済み行（データ源に依存しないキー。ROW_KEYS 参照。無い値は None で、
            None の指標を使うトリガーはそのサイクルでは評価しない）
  now     : unix 秒
  cfg     : config.json

設計の要:
  - サージ（S）は「いま急増しているか」を見るので、クールダウンが明ければ何度でも再発火する
    （以前盛り上がった銘柄がまた盛り上がったら、また通知してよい）。
  - マイルストーン（M）は「自分の snapshots で上向きの跨ぎを観測した」ときだけ・各段 生涯 1 回。
    初認識での発火はしない（trending 上位 100 ＝ 小さいチェーンではほぼ全銘柄なので、
    「その段に居る」は盛り上がりの証拠にならない）。
"""
import math
from dataclasses import dataclass

SURGES = ("S1", "S2", "S3", "S4", "S5", "S6")
MILESTONES = ("M1", "M2", "M3", "M4")
TRIGGERS = SURGES + MILESTONES
MAX_HEAT = len(TRIGGERS)

# 正規化行のキー。どのデータ源のアダプタもこの形で返す（取れない値は None）。
# mcap_is_fdv は「時価総額が取れず FDV で代用した」印（通知本文で FDV と表記する）。
ROW_KEYS = (
    "chain", "address", "symbol", "name", "price", "mcap", "mcap_is_fdv", "liquidity",
    "buys5m", "sells5m", "buyers5m", "vol5m", "vol24h", "pc5m", "pc1h", "pc24h",
    "holders", "smart", "renowned", "visiting", "created_ts", "launchpad",
    "wash", "honeypot", "dex", "source",
)


@dataclass
class Event:
    trigger: str
    stage: str = ""
    value: float = 0.0
    text: str = ""
    # ラダーの段を「消費した」だけの行は notify=False（記録するが通知にも熱度にも数えない）
    notify: bool = True
    heat: int = 0
    yabai: bool = False


# --- 数値・表示のヘルパー --------------------------------------------------

def num(v):
    """GMGN は数値を文字列で返すことがあるので緩く float 化する。失敗は None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def truthy(v):
    """GMGN が bool を文字列で返すことがあるので "false"/"0" は False として扱う。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "none", "null")
    return bool(v)


def _scaled(v, unit, div):
    s = v / div
    body = f"{s:.0f}" if abs(s) >= 100 else f"{s:.1f}".rstrip("0").rstrip(".")
    return body + unit


def fmt_usd(v):
    v = num(v)
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1e9:
        return "$" + _scaled(v, "B", 1e9)
    if a >= 1e6:
        return "$" + _scaled(v, "M", 1e6)
    if a >= 1e3:
        return "$" + _scaled(v, "K", 1e3)
    return f"${v:,.0f}" if a >= 1 else f"${v:.4g}"


def fmt_x(v):
    v = num(v)
    if v is None:
        return "?"
    return (f"{v:.0f}x" if v >= 100 else f"{v:.1f}x".replace(".0x", "x"))


def fmt_dur(sec):
    sec = num(sec)
    if sec is None or sec < 0:
        return "?"
    if sec < 60:
        return "1 分未満"
    if sec < 3600:
        return f"{int(sec // 60)} 分"
    if sec < 86400:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)} 日"


def fmt_gap(sec):
    """タイムラインの「前の段からの経過」。1 分未満は秒、1 分以上は m/s（欠けは ?）。

    送信まわり（秒の小数を持つ段）は 1 秒未満の差が意味を持つので小数第 1 位まで出す。
    秒単位でしか記録していない段（作成・観測・判定）は整数のまま出す。
    """
    sec = num(sec)
    if sec is None or sec < 0:
        return "?"
    if sec < 60:
        return f"+{int(sec)}s" if sec == int(sec) else f"+{sec:.1f}s"
    if sec < 3600:
        return f"+{int(sec // 60)}m{int(sec % 60):02d}s"
    return f"+{int(sec // 3600)}h{int(sec % 3600 // 60):02d}m"


def fmt_pct(v, digits=0):
    v = num(v)
    if v is None:
        return "?"
    return f"{v:+.{digits}f}%"


def skey(stage):
    f = float(stage)
    return str(int(f)) if f.is_integer() else repr(f)


def _last_ts(history, trig):
    return (history.get("fired", {}).get(trig) or {}).get("last_ts")


def _cooled(history, trig, now, hours):
    """銘柄 × トリガー単位のクールダウン。明けていれば再発火してよい。"""
    last = _last_ts(history, trig)
    return last is None or (now - last) >= hours * 3600


def _stages(history, trig):
    return (history.get("fired", {}).get(trig) or {}).get("stages") or set()


def baseline_snapshot(snapshots, now, window_min, field="holders"):
    """window_min 分前（±の許容幅つき）に最も近い、field を持つスナップショット。"""
    lo = now - (window_min + 15) * 60
    hi = now - (window_min - 5) * 60
    target = now - window_min * 60
    best = None
    for s in snapshots:
        ts, v = s.get("ts"), s.get(field)
        if ts is None or v is None or not (lo <= ts <= hi):
            continue
        if best is None or abs(ts - target) < abs(best["ts"] - target):
            best = s
    return best


def window_mean(snapshots, now, from_min, to_min, field):
    """now の from_min〜to_min 分前にある field の平均。1 件も無ければ None。"""
    lo, hi = now - to_min * 60, now - from_min * 60
    vals = [num(s.get(field)) for s in snapshots
            if s.get("ts") is not None and lo <= s["ts"] <= hi and s.get(field) is not None]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def prev_value(snapshots, field):
    """field を持つ直近のスナップショットの値（跨ぎ判定の「前回」）。"""
    for s in reversed(snapshots):
        v = num(s.get(field))
        if v is not None:
            return v
    return None


# --- ラダー ----------------------------------------------------------------

def _ladder_events(trig, stages, value, prev, fired_stages, rising, text_fn):
    """跨ぎを観測した最上段だけ通知し、残りの未発火段は「消費した」印として記録する。

    prev が None（初認識・その指標の前回値が無い）ときは跨ぎを観測していないので通知しない。
    rising（直近 1h が上昇中）でないときも通知しない。どちらの場合も段は消費する
    ＝ その段は生涯 1 回きりで、以後は上の段だけを見る。
    """
    cands = sorted(s for s in stages if value >= s and skey(s) not in fired_stages)
    if not cands:
        return []
    crossed = [s for s in cands if prev is not None and prev < s]
    hit = max(crossed) if (crossed and rising) else None
    out = []
    if hit is not None:
        out.append(Event(trig, skey(hit), float(value), text_fn(hit)))
    out += [Event(trig, skey(s), float(s), "", notify=False) for s in cands if s != hit]
    return out


def _reached(events, trig, threshold):
    """今回そのトリガーで threshold 以上の段に到達したか（【やばい】判定用）。"""
    return any(e.notify and e.trigger == trig and e.stage and float(e.stage) >= threshold
               for e in events)


# --- 本体 -----------------------------------------------------------------

def passes_filters(row, cfg):
    """共通フィルタ。ここを通った銘柄だけ S/M を評価する。

    - 低流動性・巨大時価総額・保有者が少なすぎる銘柄を除外（値が取れない項目では除外しない）。
    - `size_any` は「規模の下限」。時価総額・24h 出来高・5 分出来高の**いずれか 1 つ**が
      下限以上であることを求める（値が None の項目は不成立。全部 None なら評価しない）。
      Tomato の関心は「結構でかいやつ」なので、$50K 未満の小型を全トリガーの手前で落とす。
    - is_honeypot は通知本文に出すだけで除外しない。
    """
    filters = cfg.get("filters", {})
    for key, value, worse in (
        ("min_liquidity_usd", num(row.get("liquidity")), lambda v, g: v < g),
        ("min_holders", num(row.get("holders")), lambda v, g: v < g),
        ("max_market_cap_usd", num(row.get("mcap")), lambda v, g: v > g),
    ):
        gate = num(filters.get(key))
        if value is not None and gate is not None and worse(value, gate):
            return False
    size = filters.get("size_any") or {}
    if size:
        big = [num(row.get(k)) is not None and num(gate) is not None
               and num(row.get(k)) >= num(gate) for k, gate in size.items()]
        if not any(big):
            return False
    return True


def evaluate(token, history, row, now, cfg):
    if not passes_filters(row, cfg):
        return []
    mcap = num(row.get("mcap"))
    price = num(row.get("price"))
    pc5m, pc1h, pc24h = num(row.get("pc5m")), num(row.get("pc1h")), num(row.get("pc24h"))
    buys, sells = num(row.get("buys5m")), num(row.get("sells5m"))
    holders = num(row.get("holders"))
    smart = num(row.get("smart"))
    visiting = num(row.get("visiting"))
    vol5m = num(row.get("vol5m"))
    vol24h = num(row.get("vol24h"))

    creation_ts = num(token.get("creation_ts")) or num(row.get("created_ts"))
    age_days = (now - creation_ts) / 86400.0 if creation_ts else None
    first_price = num(token.get("first_price"))
    first_mcap = num(token.get("first_mcap"))
    first_seen = num(token.get("first_seen_ts"))
    mult = price / first_price if (price is not None and first_price) else None
    snaps = history.get("snapshots", [])
    events = []

    # --- S1 価格急騰 --------------------------------------------------------
    s1 = cfg.get("s1", {})
    hot_1h = pc1h is not None and pc1h >= s1.get("pc1h_pct", 80)
    hot_5m = pc5m is not None and pc5m >= s1.get("pc5m_pct", 25)
    if ((hot_1h or hot_5m)
            and buys is not None and buys >= s1.get("min_buys_5m", 30)
            and _cooled(history, "S1", now, s1.get("cooldown_h", 2))):
        shown = f"1h {fmt_pct(pc1h)}" if hot_1h else f"5 分 {fmt_pct(pc5m)}"
        events.append(Event("S1", "", pc1h if hot_1h else pc5m, f"価格急騰 {shown}"))

    # --- S2 出来高急増（平常時の 5 分出来高との比） -----------------------------
    s2 = cfg.get("s2", {})
    if (vol5m is not None and vol5m >= s2.get("min_vol5m_usd", 20000)
            and _cooled(history, "S2", now, s2.get("cooldown_h", 2))):
        base = window_mean(snaps, now, s2.get("baseline_from_min", 15),
                           s2.get("baseline_to_min", 75), "vol5m")
        if base is None:
            # 平常値を自分で観測していない（初認識）ときは絶対値だけで判断する
            if vol5m >= s2.get("no_baseline_vol5m_usd", 60000):
                events.append(Event("S2", "", vol5m,
                                    f"出来高急増 5 分 {fmt_usd(vol5m)}（初認識）"))
        elif vol5m >= s2.get("baseline_mult", 3) * base:
            ratio = f"平常比 {fmt_x(vol5m / base)}" if base > 0 else "平常ほぼ 0 から"
            events.append(Event("S2", "", vol5m, f"出来高急増 5 分 {fmt_usd(vol5m)}（{ratio}）"))

    # --- S3 殺到（wash 疑いは抑止） -------------------------------------------
    # 買い回数は 1 人が何度も撃てるので、買い手の実数（buyers5m）が取れるときはそちらを見る
    s3 = cfg.get("s3", {})
    buyers = num(row.get("buyers5m"))
    if buyers is not None:
        rush, value, shown = (buyers >= s3.get("min_buyers_5m", 150), buyers,
                              f"買い手 {int(buyers):,} 人")
    else:
        rush, value, shown = (buys is not None and buys >= s3.get("min_buys_5m", 300), buys,
                              f"買 {int(buys):,}" if buys is not None else "")
    if (rush
            and buys is not None and sells is not None
            and buys >= s3.get("buy_sell_ratio", 1.0) * sells
            and not truthy(row.get("wash"))
            and _cooled(history, "S3", now, s3.get("cooldown_h", 2))):
        events.append(Event("S3", "", value, f"殺到 5 分で{shown}"))

    # --- S4 保有者急増 -------------------------------------------------------
    s4 = cfg.get("s4", {})
    win4 = s4.get("window_min", 30)
    base4 = baseline_snapshot(snaps, now, win4, "holders")
    b4 = num(base4.get("holders")) if base4 else None
    if holders is not None and b4:
        gain = (holders - b4) / b4 * 100
        if (gain >= s4.get("pct", 15) and holders - b4 >= s4.get("abs", 30)
                and _cooled(history, "S4", now, s4.get("cooldown_h", 2))):
            events.append(Event("S4", "", gain,
                                f"保有者急増 {fmt_pct(gain)}/{win4} 分"
                                f"（{int(b4):,}→{int(holders):,}）"))

    # --- S5 スマートマネー流入 -------------------------------------------------
    s5 = cfg.get("s5", {})
    base5 = baseline_snapshot(snaps, now, s5.get("window_min", 30), "smart")
    prev_smart = num(base5.get("smart")) if base5 else None
    if (smart is not None and smart >= s5.get("min_smart", 5)
            and prev_smart is not None and smart - prev_smart >= s5.get("min_increase", 3)
            and _cooled(history, "S5", now, s5.get("cooldown_h", 6))):
        events.append(Event("S5", "", smart,
                            f"スマートマネー +{int(smart - prev_smart)}（今 {int(smart)}）"))

    # --- S6 注目急増（順位ではなく閲覧数そのものの伸び） --------------------------
    s6 = cfg.get("s6", {})
    win6 = s6.get("window_min", 30)
    base6 = baseline_snapshot(snaps, now, win6, "visiting")
    prev_vis = num(base6.get("visiting")) if base6 else None
    if (visiting is not None and visiting >= s6.get("min_visiting", 300)
            and prev_vis is not None and visiting >= s6.get("ratio", 2) * prev_vis
            and _cooled(history, "S6", now, s6.get("cooldown_h", 6))):
        events.append(Event("S6", "", visiting,
                            f"注目急増 {int(visiting):,}（{win6} 分前 {int(prev_vis):,}）"))

    # --- M1〜M4（跨ぎを観測 かつ 直近 1h が上昇中 かつ いま動いているときだけ） --------
    ms = cfg.get("milestone", {})
    rising = pc1h is not None and pc1h > ms.get("min_pc1h_pct", 0)
    # 「3x 到達」でも 5 分出来高 $210・買 2 のような静かな銘柄は撃たない（2026-09-18 実測）
    lively = (vol5m is not None and vol5m >= ms.get("min_vol5m_usd", 2000)
              and buys is not None and buys >= ms.get("min_buys_5m", 5))

    m1 = cfg.get("m1", {})
    if lively and mcap is not None and age_days is not None and age_days <= m1.get("max_age_days", 30):
        ctx = ""
        if first_seen and now - first_seen >= 120:
            ctx = f"初認識 {fmt_usd(first_mcap)}" if first_mcap else ""
            if mult is not None:
                ctx += (" → " if ctx else "") + fmt_x(mult)
            ctx += (" / " if ctx else "") + fmt_dur(now - first_seen)
        events += _ladder_events(
            "M1", m1.get("ladder", []), mcap, prev_value(snaps, "mcap"),
            _stages(history, "M1"), rising,
            lambda s: f"時価総額 {fmt_usd(s)} 到達" + (f"（{ctx}）" if ctx else ""))

    m2 = cfg.get("m2", {})
    if lively and mult is not None:
        pp = prev_value(snaps, "price")
        events += _ladder_events(
            "M2", m2.get("ladder", []), mult, (pp / first_price) if pp is not None else None,
            _stages(history, "M2"), rising, lambda s: f"{skey(s)}x 到達")

    m3 = cfg.get("m3", {})
    if lively and vol24h is not None and age_days is not None and age_days <= m3.get("max_age_days", 30):
        events += _ladder_events(
            "M3", m3.get("ladder", []), vol24h, prev_value(snaps, "vol24h"),
            _stages(history, "M3"), rising,
            lambda s: f"24h 出来高 {fmt_usd(s)} 到達（今 {fmt_usd(vol24h)}）")

    # --- M4 古参の復活 -------------------------------------------------------
    m4 = cfg.get("m4", {})
    if (lively and rising and age_days is not None and age_days > m4.get("min_age_days", 30)
            and pc24h is not None and pc24h >= m4.get("min_change_24h_pct", 200)
            and _cooled(history, "M4", now, m4.get("cooldown_h", 24))):
        events.append(Event("M4", "", pc24h, f"古参復活 24h {fmt_pct(pc24h)}"))

    if not events:
        return []

    # 直近 24h の発火にはルール変更前のトリガー名（旧 T1〜T8）が混ざりうるので、
    # 今のトリガー種別だけを数える
    recent = set(history.get("recent_triggers", ())) & set(TRIGGERS)
    heat = min(len({e.trigger for e in events if e.notify} | recent), MAX_HEAT)
    y = cfg.get("yabai", {})
    yabai = (
        _reached(events, "M1", y.get("mcap", 1e6))
        or _reached(events, "M2", y.get("x", 10))
        or _reached(events, "M3", y.get("volume", 1e6))
        or heat >= y.get("heat", 4)
    )
    for e in events:
        e.heat, e.yabai = heat, bool(yabai)
    return events


# --- EMS（Early Momentum Score, §11.3 / §12.3）------------------------------
#
# 旧ルール（S/M）とは独立に動く。固定閾値ではなく「その銘柄の平常値からの外れ」を
# 100 点に写すので、静かな銘柄の買い手 3 人と賑やかな銘柄の 3 人を同じ土俵で比べられる。

LEVEL_CUTS = {"WATCH": 60, "HOT": 70, "STRONG": 80, "EXTREME": 90}
LEVELS = ("WATCH", "HOT", "STRONG", "EXTREME")   # 低い順
WEIGHTS = {"vol": 30, "buyers": 25, "flow": 20, "accel": 10, "wallet": 10, "price": 5}
FLOW_GRAD = 0.05     # 卒業前は流動性の代わりに卒業必要額の何割で満点にするか
MAD_FLOOR = 0.5      # 0 除算と「平常が凪だと全部満点」を防ぐ


def clip01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


def poisson_g(k, mu):
    """観測 k 人が平常 μ 人からどれだけ外れているか（ポアソン尤度比の sqrt(2·LLR)）。

    件数そのものではなく驚きの大きさなので銘柄をまたいで比較できる。k ≤ μ なら 0。
    """
    if mu <= 0 or k <= mu:
        return 0.0
    return math.sqrt(2 * (k * math.log(k / mu) - (k - mu)))


def level_of(value, cuts):
    lv = ""
    for name in LEVELS:
        if value >= num(cuts.get(name, LEVEL_CUTS[name])):
            lv = name
    return lv


def _wallet_history(ctx):
    """c_wallet の履歴ぶん。S4/S5/S6 と同じ「30 分前スナップショット」の見方を流用する。"""
    snaps, now = ctx.get("snapshots") or [], num(ctx.get("now")) or 0
    def prev(field):
        return num((baseline_snapshot(snaps, now, 30, field) or {}).get(field))
    smart, psmart = num(ctx.get("smart")), prev("smart")
    holders, pholders = num(ctx.get("holders")), prev("holders")
    vis, pvis = num(ctx.get("visiting")), prev("visiting")
    return {
        "smart": bool(smart is not None and psmart is not None and smart - psmart >= 2),
        "holders": bool(holders is not None and pholders
                        and (holders - pholders) / pholders * 100 >= 15),
        "visiting": bool(vis is not None and pvis and vis >= 2 * pvis),
    }


def score(feat, cfg, ctx):
    """特徴量から EMS を出す純粋関数。DB にも時計にもネットにも触らない。

    feat: chain/agg.py features()（または API レーンの簡易版）。窓の並びは feat["wins"]、
          流入と加速に使う窓は feat["flow_w"] で渡す（チェーン 30 秒 / API 300 秒）。
    ctx : 採点に要る周辺情報（liquidity / mcap / wash / honeypot / grad_usd と、
          c_wallet 用の snapshots・holders・smart・visiting・now）。
    """
    sc = cfg.get("score", {})
    g = num(sc.get("gain")) or 1.0
    kappa = num(sc.get("kappa"))
    kappa = 0.5 if kappa is None else kappa
    cuts = {**LEVEL_CUTS, **(sc.get("levels") or {})}
    out = {"score": 0.0, "level": "", "reason": "", "parts": {}, "points": {},
           "penalties": [], "penalty": 0.0, "progress": None, "g_accel": None}

    if truthy(ctx.get("honeypot")):
        out["reason"] = "honeypot"
        return out

    wins = [int(w) for w in (feat.get("wins") or (10, 30, 120))]
    flow_w, long_w = int(feat.get("flow_w") or 30), wins[-1]
    liq, mcap = num(ctx.get("liquidity")), num(ctx.get("mcap"))
    grad, age = num(ctx.get("grad_usd")), num(feat.get("age_min"))
    raised = num(feat.get("raised_usd"))
    progress = raised / grad if (raised is not None and grad) else None
    # 卒業前は流動性プールが無く L で測れないので、カーブの必要額を物差しにする
    pre_grad = (feat.get("launchpad") == "four.meme" and progress is not None
                and progress < 1)
    out["progress"] = progress

    if pre_grad:
        passed = (num(feat.get(f"U{long_w}")) or 0) >= 5 and not feat.get("bundle")
    else:
        need = num(sc.get("min_liquidity_new_usd", 10000)) if (
            age is not None and age < (num(sc.get("new_token_min")) or 60)
        ) else num(sc.get("min_liquidity_usd", 20000))
        passed = liq is not None and need is not None and liq >= need
    if not passed:
        out["reason"] = "gate"
        return out

    # 市場全体が沸いている時間帯は全銘柄が同時に鳴るので、その分を差し引く
    adj = kappa * max(num(feat.get("z_market")) or 0.0, 0.0)
    med = num(feat.get("med")) or 0.0
    mad = max(MAD_FLOOR, num(feat.get("mad")) or 0.0)
    mu_rate = num(feat.get("mu_rate")) or 0.0
    zs, gs = {}, {}
    for w in wins:
        vb = num(feat.get(f"Vb{w}"))
        if vb is not None:
            zs[w] = (math.log1p(vb * 60.0 / w) - med) / mad - adj
        u = num(feat.get(f"U{w}"))
        if u is not None:
            gs[w] = poisson_g(u, mu_rate * w / 60.0) - adj

    u30, u120 = num(feat.get("U30")), num(feat.get("U120"))
    if u30 is None and u120 is None:
        # 5 分窓しか無い API レーンは、その窓の買い手数で最低証拠を代用する
        evidence = max([num(feat.get(f"U{w}")) or 0 for w in wins] or [0]) >= 5
    else:
        evidence = (u30 or 0) >= 3 or (u120 or 0) >= 5

    c_vol = clip01(max(zs.values()) / (4 * g)) if zs else 0.0
    c_buyers = clip01(max(gs.values()) / (4.3 * g)) if (gs and evidence) else 0.0

    bsi = max(num(feat.get("bsi")) or 0.0, 0.0)
    net = num(feat.get(f"Net{flow_w}"))
    frac = num(feat.get("flow_frac")) or 0.02
    denom = FLOW_GRAD * g * grad if (pre_grad and grad) else (frac * g * liq if liq else None)
    c_flow = 0.3 * bsi
    if denom and net is not None:
        c_flow += 0.7 * clip01(net / denom)

    g_now, g_prev = gs.get(flow_w), num(feat.get("g30_prev"))
    out["g_accel"] = g_now
    c_accel = clip01((g_now - g_prev) / 2) if (
        evidence and g_now is not None and g_prev is not None) else 0.0

    wal = _wallet_history(ctx)
    c_wallet = min(1.0, 0.4 * (num(feat.get("n_listed_10m")) or 0)
                   + 0.2 * wal["smart"] + 0.2 * wal["holders"] + 0.2 * wal["visiting"])

    pc = num(feat.get("pc120"))
    net_long = num(feat.get(f"Net{long_w}")) or 0.0
    # 時価総額に対して薄い板ほど値動きは当てにならないので価格の重みを落とす
    w_price = min(1.0, max(0.3, 100.0 * liq / mcap)) if (liq and mcap) else 1.0
    c_price = clip01((pc or 0.0) / 10.0) * w_price if (pc is not None and net_long > 0) else 0.0

    b300, u300 = num(feat.get("B300")) or 0.0, num(feat.get("U300")) or 0.0
    vb_l, vs_l = num(feat.get(f"Vb{long_w}")) or 0.0, num(feat.get(f"Vs{long_w}")) or 0.0
    pens = []
    if b300 >= 10 and u300 / b300 < 0.3:
        pens.append(("same_wallet", 15))
    if b300 >= 3 and (num(feat.get("Top1_300")) or 0.0) > 0.5:
        pens.append(("top1", 10))
    if feat.get("bundle"):
        pens.append(("bundle", 10))
    if truthy(ctx.get("wash")):
        pens.append(("wash", 20))
    if (num(feat.get("roundtrip_300")) or 0.0) > 0.5:
        pens.append(("roundtrip", 10))
    if vs_l >= vb_l > 0 and (pc or 0.0) < 0:
        pens.append(("dump", 10))
    penalty = float(sum(p for _, p in pens))

    parts = {"vol": c_vol, "buyers": c_buyers, "flow": clip01(c_flow), "accel": c_accel,
             "wallet": c_wallet, "price": c_price}
    points = {k: WEIGHTS[k] * v for k, v in parts.items()}
    total = max(0.0, min(100.0, sum(points.values()) - penalty))
    out.update({"score": total, "level": level_of(total, cuts), "parts": parts,
                "points": points, "penalties": [n for n, _ in pens], "penalty": penalty,
                "z": zs, "g": gs, "evidence": evidence, "pre_grad": pre_grad,
                "wallet_hits": wal})
    return out
