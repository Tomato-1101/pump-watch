"""Telegram の常駐 bot。ボタン操作と「押したら即返す」だけを担当する。

判定・通知は watch.py（API レーン）と chain_watch.py（高速レーン）がやる。ここは
getUpdates のロングポーリングに専念する。**getUpdates の消費者は 1 つしか持てない**
（複数で叩くと 409 になる）ので、巡回側からは呼ばない約束にしてある。

返信は必ず 1 往復で返す（DB を読むだけ・外部 API を叩かない）。何か失敗しても
黙らず「エラー: …」と返す。トークンが混ざる可能性のある文字列は notify._scrub で伏せる。
"""
import sys
import time

import db
import notify
import rules
import watch

POLL_TIMEOUT = 25        # ロングポーリングの待ち（秒）
ERROR_SLEEP = 5          # getUpdates が失敗したときの間合い（暴走させない）

# / を打つと出る一覧（setMyCommands で登録する）
COMMANDS = (
    ("status", "稼働状況（送信 ON/OFF・当日の通数・レーンの生存）"),
    ("hot", "今日の盛り上がり（確定・惜しい）"),
    ("score", "直近 10 分の EMS 上位"),
    ("timeline", "その銘柄の通知の時系列（/timeline 0x…）"),
    ("mark", "この銘柄を覚えておく（/mark 0x… メモ）"),
    ("marks", "最近の /mark を一覧"),
    ("near", "発火しなかったが近かった銘柄"),
    ("chains", "対象チェーンをボタンで切り替え"),
    ("config", "現在のしきい値を一覧"),
    ("dryrun", "送信 ON/OFF を切り替え"),
    ("menu", "操作ボタンを出す"),
    ("help", "コマンド一覧"),
)

# /menu で出すボタン（メッセージに付くインライン。画面下に常設はしない）
MENU_ITEMS = ((("状態", "r:status"), ("近い銘柄", "r:near")),
              (("チェーン", "m:chains"), ("しきい値", "t")),
              (("送信 ON/OFF", "m:dryrun"), ("ヘルプ", "m:help")))
MENU_TEXT = "ボタンで操作できます。"

CHAIN_ORDER = ("bsc", "robinhood", "arc", "sol", "base", "eth")

# しきい値のセクション（キーの接頭辞 → 見出し）
SECTIONS = (
    ("filters", "フィルタ（この条件を満たさない銘柄は無視）"),
    ("s1", "S1 価格急騰"),
    ("s2", "S2 出来高急増"),
    ("s3", "S3 買い殺到"),
    ("s4", "S4 保有者増"),
    ("s5", "S5 スマートマネー"),
    ("s6", "S6 注目度"),
    ("m1", "M1 時価総額の到達"),
    ("m3", "M3 24h 出来高の到達"),
    ("m4", "M4 古参の復活"),
    ("milestone", "到達系の共通の足切り"),
    ("yabai", "「やばい」判定"),
    ("score", "EMS スコア（点数・通知の作法）"),
    ("hot", "盛り上がり（10 分周期・確定判定）"),
    ("notify", "配信（即時とまとめ）"),
    ("gt", "GeckoTerminal の呼び方"),
    ("ds", "DexScreener の呼び方"),
    ("gmgn", "GMGN の呼び方"),
    ("", "その他"),
)

# キー → (短い名前, 1 行の意味)。しきい値ボタンの説明に使う。
KEYS = {
    "filters.min_liquidity_usd": ("最低流動性", "これ未満の流動性は見ない（抜けられない板を弾く）"),
    "filters.max_market_cap_usd": ("最大時価総額", "これを超える大型は見ない"),
    "filters.min_holders": ("最低保有者数", "保有者がこれ未満は見ない（取れたときだけ判定）"),
    "filters.size_any.mcap": ("規模: 時価総額", "この 3 つのどれかを満たせば「見る価値あり」"),
    "filters.size_any.vol24h": ("規模: 24h 出来高", "この 3 つのどれかを満たせば「見る価値あり」"),
    "filters.size_any.vol5m": ("規模: 5 分出来高", "この 3 つのどれかを満たせば「見る価値あり」"),
    "s1.pc1h_pct": ("S1 1h 変化率", "1 時間でこの % 以上上がっていること"),
    "s1.pc5m_pct": ("S1 5 分変化率", "5 分でこの % 以上上がっていること"),
    "s1.min_buys_5m": ("S1 5 分の買い回数", "5 分の買いがこの回数以上あること"),
    "s1.cooldown_h": ("S1 再発火まで", "同じ銘柄で次に S1 を出すまでの時間"),
    "s2.min_vol5m_usd": ("S2 最低 5 分出来高", "平常比を見る前の足切り"),
    "s2.baseline_mult": ("S2 平常比", "平常時の何倍で発火するか"),
    "s2.baseline_from_min": ("S2 平常の始点", "何分前からを「平常」とみなすか"),
    "s2.baseline_to_min": ("S2 平常の終点", "何分前までを「平常」とみなすか"),
    "s2.no_baseline_vol5m_usd": ("S2 平常なしの閾値", "平常が取れないとき、この額だけで発火"),
    "s2.cooldown_h": ("S2 再発火まで", "同じ銘柄で次に S2 を出すまでの時間"),
    "s3.min_buys_5m": ("S3 5 分の買い回数", "5 分の買い回数がこれ以上"),
    "s3.min_buyers_5m": ("S3 5 分の買い人数", "5 分の買い手がこの人数以上"),
    "s3.buy_sell_ratio": ("S3 買い/売り比", "買いが売りの何倍以上か"),
    "s3.cooldown_h": ("S3 再発火まで", "同じ銘柄で次に S3 を出すまでの時間"),
    "s4.window_min": ("S4 観測窓", "保有者の増加を何分で見るか"),
    "s4.pct": ("S4 増加率", "窓の間にこの % 以上増えたか"),
    "s4.abs": ("S4 増加人数", "窓の間にこの人数以上増えたか"),
    "s4.cooldown_h": ("S4 再発火まで", "同じ銘柄で次に S4 を出すまでの時間"),
    "s5.min_smart": ("S5 スマート人数", "スマートマネーがこの人数以上"),
    "s5.window_min": ("S5 観測窓", "増加を何分で見るか"),
    "s5.min_increase": ("S5 増加人数", "窓の間にこの人数以上増えたか"),
    "s5.cooldown_h": ("S5 再発火まで", "同じ銘柄で次に S5 を出すまでの時間"),
    "s6.window_min": ("S6 観測窓", "注目度の伸びを何分で見るか"),
    "s6.ratio": ("S6 伸び倍率", "窓の間に何倍になったか"),
    "s6.min_visiting": ("S6 最低注目度", "GMGN の閲覧数がこれ以上"),
    "s6.cooldown_h": ("S6 再発火まで", "同じ銘柄で次に S6 を出すまでの時間"),
    "m1.max_age_days": ("M1 対象の新しさ", "作成からこの日数以内の銘柄だけ"),
    "m3.max_age_days": ("M3 対象の新しさ", "作成からこの日数以内の銘柄だけ"),
    "m4.min_age_days": ("M4 対象の古さ", "作成からこの日数以上たった銘柄だけ"),
    "m4.min_change_24h_pct": ("M4 24h 変化率", "24 時間でこの % 以上上がったか"),
    "m4.cooldown_h": ("M4 再発火まで", "同じ銘柄で次に M4 を出すまでの時間"),
    "milestone.min_pc1h_pct": ("到達: 1h 変化率", "到達系はこの % 以上上がっていないと出さない"),
    "milestone.min_vol5m_usd": ("到達: 5 分出来高", "到達系はこの出来高が無いと出さない"),
    "milestone.min_buys_5m": ("到達: 5 分の買い回数", "到達系はこの買い回数が無いと出さない"),
    "yabai.mcap": ("やばい: 時価総額", "これ以上なら見出しに【やばい】を付ける"),
    "yabai.volume": ("やばい: 出来高", "24h 出来高がこれ以上なら【やばい】"),
    "yabai.x": ("やばい: 初認識比", "初めて見たときの何倍で【やばい】"),
    "yabai.heat": ("やばい: 熱度", "熱度がこれ以上なら【やばい】"),
    "notify.immediate.mcap": ("即時: 時価総額",
                              "これ以上なら待たずに 1 通で送る（まとめ間隔が 0 のときは効かない＝常に即時）"),
    "notify.immediate.vol24h": ("即時: 24h 出来高",
                                "これ以上なら待たずに 1 通で送る（まとめ間隔が 0 のときは効かない＝常に即時）"),
    "notify.immediate_cooldown_min": ("即時の間隔",
                                      "同じ銘柄を即時で送る最短間隔（分）（まとめ間隔が 0 のときは効かない）"),
    "notify.batch_minutes": ("まとめの間隔",
                             "0 で全部即時（まとめ無し・既定）。分を入れると即時でないものをまとめる"),
    "notify.max_pending": ("まとめの上限", "この件数たまったら間隔前でも送る（まとめ間隔が 0 のときは効かない）"),
    "gt.max_calls_per_min": ("GT 1 分あたり", "GeckoTerminal を 1 分に何回まで叩くか"),
    "gt.min_gap_seconds": ("GT 呼び出し間隔", "連続で叩くときの最短間隔（秒）"),
    "gt.block_seconds": ("GT 待ち時間", "429 を食らったあと何秒休むか"),
    "ds.workers": ("DS 並列数", "DexScreener を何本並列で叩くか"),
    "ds.active_hours": ("DS 対象の鮮度", "何時間以内に動いた銘柄を更新対象にするか"),
    "gmgn.interval_24h_every": ("GMGN 24h の頻度", "何周期に 1 回 24h 側も取るか"),
    "score.gain": ("EMS 感度", "満点の物差し。上げると鳴りにくく、下げると鳴りやすい（調整はここだけ）"),
    "score.kappa": ("EMS 市場補正", "チェーン全体が沸いている分を差し引く強さ"),
    "score.levels.WATCH": ("👀 WATCH の点", "この点数以上で 👀 WATCH（カットは原則動かさない）"),
    "score.levels.HOT": ("🔥 HOT の点", "この点数以上で 🔥 HOT（カットは原則動かさない）"),
    "score.levels.STRONG": ("🚨 STRONG の点", "この点数以上で 🚨 STRONG（カットは原則動かさない）"),
    "score.levels.EXTREME": ("💥 EXTREME の点", "この点数以上で 💥 EXTREME（カットは原則動かさない）"),
    "score.cooldown_min.WATCH": ("👀 WATCH の再通知間隔", "同じレベルで次に送るまでの分"),
    "score.cooldown_min.HOT": ("🔥 HOT の再通知間隔", "同じレベルで次に送るまでの分"),
    "score.cooldown_min.STRONG": ("🚨 STRONG の再通知間隔", "同じレベルで次に送るまでの分"),
    "score.cooldown_min.EXTREME": ("💥 EXTREME の再通知間隔", "同じレベルで次に送るまでの分"),
    "score.renotify_delta": ("再通知の上乗せ", "同じレベルで再び送るのに要る点差"),
    "score.min_liquidity_usd": ("EMS 最低流動性", "これ未満の銘柄は採点しない"),
    "score.min_liquidity_new_usd": ("EMS 最低流動性（新規）", "生後が浅い銘柄に使う下限"),
    "score.new_token_min": ("EMS 新規の定義", "生後この分までを「新規」として扱う"),
    "score.fourmeme_grad_bnb": ("卒業の必要 BNB", "four.meme のカーブ卒業に要る BNB（流入の物差し）"),
    "score.log_min_score": ("JSONL に残す点", "この点以上は採点ログに残す"),
    "score.db_min_score": ("DB に残す点", "この点以上は scores 表にも残す"),
    "score.retention_days": ("scores の保存日数", "採点結果を何日で捨てるか"),
    "hot.interval_sec": ("盛り上がり: 巡回の間隔", "launchd の起動間隔と同じ値（秒）。参考値"),
    "hot.api_gap_sec": ("盛り上がり: 呼び出し間隔", "GMGN を連続で叩くときの最短間隔（秒）"),
    "hot.size.swaps": ("盛り上がり: 取引数",
                       "24h の取引がこの回数以上（規模は取引・出来高・保有者・ピークの 4 つ全部）。保有者が多い銘柄は代替側"
                       "（hot.size.alt_holders 人以上なら hot.size.alt_swaps 回以上）でも可"),
    "hot.size.volume_usd": ("盛り上がり: 出来高",
                            "24h の出来高がこの額以上。取引の代替（hot.size.alt_holders 人以上・"
                            "hot.size.alt_swaps 回以上）を満たしピークも hot.size.alt_peak_mcap_usd "
                            "以上の銘柄は、hot.size.alt_volume_usd 以上でも可"),
    "hot.size.holders": ("盛り上がり: 保有者数", "保有者がこの人数以上"),
    "hot.size.alt_holders": ("盛り上がり: 取引の代替・保有者数",
                             "保有者がこの人数以上なら、取引数は「代替の取引数」以上で足りる"),
    "hot.size.alt_swaps": ("盛り上がり: 取引の代替・取引数", "保有者が代替の人数以上のときに要る 24h の取引回数"),
    "hot.size.peak_mcap_usd": ("盛り上がり: ピーク時価総額",
                               "今日（JST 0 時から）観測した時価総額の最高値がこの額に届かなければ確定しない"),
    "hot.size.alt_volume_usd": ("盛り上がり: 出来高の代替・出来高",
                                "取引の代替が成り立ちピークも hot.size.alt_peak_mcap_usd 以上のとき、"
                                "出来高はこの額以上で足りる"),
    "hot.size.alt_peak_mcap_usd": ("盛り上がり: 出来高の代替・ピーク",
                                   "出来高の代替（hot.size.alt_volume_usd）に要るピーク時価総額の下限"),
    "hot.burst.chg24_pct": ("盛り上がり: 24h 変化率", "24h でこの % 以上上がった（勢いはどれか 1 つ）"),
    "hot.burst.big_swaps": ("盛り上がり: 大量取引", "取引がこの回数を超えるなら緩い変化率で認める"),
    "hot.burst.big_chg24_pct": ("盛り上がり: 大量時の変化率", "大量取引のときに要る 24h の %"),
    "hot.burst.day_range_x": ("盛り上がり: 今日の値幅", "自前観測の安値→その後の高値がこの倍率以上"),
    "hot.burst.day_range_chg24_pct": ("盛り上がり: 値幅のときの変化率", "今日の値幅で通すときに要る 24h の %"),
    "hot.burst.holder_growth": ("盛り上がり: 保有者の増加", "今日の最初の観測からこの人数以上増えた"),
    "hot.burst.holder_growth_pct": ("盛り上がり: 保有者の伸び率",
                                    "今日の最初の観測からこの % 以上増えた（人数と両方要る）"),
    "hot.burst.holder_chg24_pct": ("盛り上がり: 保有者増のときの変化率", "保有者の増加で通すときに要る 24h の %"),
    "hot.confirm_passes": ("盛り上がり: 確定に要る回数", "連続で何周期通過したら確定にするか"),
    "hot.cooldown_hours": ("盛り上がり: 再通知まで", "同じ銘柄を再び通知するまでの時間"),
    "hot.near_limit": ("盛り上がり: 惜しいの件数", "日次まとめに出す「惜しい」の行数"),
    "hot.retention_days": ("盛り上がり: 観測の保存日数", "hot_obs を何日で捨てるか"),
    "snapshot_min_gap_seconds": ("記録の間隔", "同じ銘柄のスナップショットを取る最短間隔（秒）"),
    "snapshot_retention_days": ("記録の保存日数", "スナップショットを何日で捨てるか"),
}

STEPS = ((-50, "−50%"), (-20, "−20%"), (-10, "−10%"), (10, "+10%"), (20, "+20%"), (50, "+50%"))


# --- 設定のキー -------------------------------------------------------------

def numeric_keys(cfg, prefix=""):
    """cfg の中で /set が受け付ける数値キー（入れ子は s2.baseline_mult の形）。"""
    out = []
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out += numeric_keys(v, key + ".")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(key)
    return out


def section_of(key):
    """キーがどのセクションに属するか（先頭のかたまり。無ければ「その他」）。"""
    head = key.split(".")[0]
    return head if any(head == s for s, _ in SECTIONS) else ""


def get_value(cfg, key):
    cur = cfg
    for p in key.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None


def default_value(key):
    return get_value(watch.DEFAULTS, key)


def step_value(old, pct):
    """old を pct% 動かす。元が整数なら整数のまま（0 や負にはしない）。"""
    new = old * (1 + pct / 100.0)
    if isinstance(old, int):
        new = int(round(new))
        return max(new, 1) if old > 0 else new
    return round(new, 4)


# --- ボタン -----------------------------------------------------------------

def chains_markup(cfg):
    cur = set(cfg.get("chains") or ())
    rows, row = [], []
    for c in CHAIN_ORDER:
        row.append({"text": ("✓ " if c in cur else "") + c, "callback_data": f"c:{c}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return notify.inline_markup(rows)


def chains_text(cfg):
    return "対象チェーン（押すと切り替え）\n今: " + (",".join(cfg.get("chains") or ()) or "なし")


def dry_button(cfg, kind="d"):
    """送信 ON/OFF のトグル 1 個。今の状態を出し、押すと反転する（2 択を出さない）。"""
    on = not cfg.get("dry_run", True)
    return {"text": "🔔 送信 ON（押すと OFF）" if on else "🔕 送信 OFF（押すと ON）",
            "callback_data": f"{kind}:toggle"}


def dryrun_markup(cfg):
    return notify.inline_markup([[dry_button(cfg)]])


def dryrun_text(cfg):
    now = "OFF（ドライラン）" if cfg.get("dry_run", True) else "ON"
    return f"通知の送信: {now}\nボタンを押すと反転。両レーンとも次の周期で反映（最大 30 秒）。"


def status_markup(cfg):
    """/status の下に送信トグルと更新を並べる（切り替えのために別画面へ行かなくて済む）。"""
    return notify.inline_markup([[dry_button(cfg, "sd")],
                                 [{"text": "更新", "callback_data": "r:status"}]])


def menu_markup():
    return notify.inline_markup([[{"text": t, "callback_data": d} for t, d in row]
                                 for row in MENU_ITEMS])


def sections_markup():
    return notify.inline_markup([[{"text": title, "callback_data": f"ts:{key}"}]
                                 for key, title in SECTIONS])


def section_markup(cfg, section):
    """セクション内のキーを 1 行 1 キー・現在値つきで並べる。"""
    rows = []
    for key in numeric_keys(cfg):
        if section_of(key) != section:
            continue
        label = KEYS.get(key, (key, ""))[0]
        rows.append([{"text": f"{label}  {watch._fmt_cfg_value(get_value(cfg, key))}",
                      "callback_data": f"tk:{key}"}])
    rows.append([{"text": "← セクション一覧", "callback_data": "t"}])
    return notify.inline_markup(rows)


def key_text(cfg, key):
    label, meaning = KEYS.get(key, (key, ""))
    cur = watch._fmt_cfg_value(get_value(cfg, key))
    dflt = watch._fmt_cfg_value(default_value(key))
    lines = [f"<b>{label}</b>（{key}）", f"今: {cur} ・ 既定: {dflt}"]
    if meaning:
        lines.append(meaning)
    return "\n".join(lines)


def key_markup(key):
    section = section_of(key)
    rows = [[{"text": t, "callback_data": f"ta:{key}:{p}"} for p, t in STEPS[:3]],
            [{"text": t, "callback_data": f"ta:{key}:{p}"} for p, t in STEPS[3:]],
            [{"text": "既定に戻す", "callback_data": f"ta:{key}:def"},
             {"text": "← 戻る", "callback_data": f"ts:{section}"}]]
    return notify.inline_markup(rows)


def refresh_markup(what):
    return notify.inline_markup([[{"text": "更新", "callback_data": f"r:{what}"}]])


# --- 受け取ったものを処理する -------------------------------------------------

def apply_step(cfg, key, op):
    """しきい値ボタン 1 回ぶんを適用して (返信文, 変わったか) を返す。"""
    old = get_value(cfg, key)
    if old is None:
        return f"エラー: 知らないキー {key}", False
    new = default_value(key) if op == "def" else step_value(old, float(op))
    if new == old:
        return f"{KEYS.get(key, (key, ''))[0]}: {watch._fmt_cfg_value(old)}（変わらず）", False
    ok, reply, updates = watch.set_config_value(cfg, key, str(new))
    if not ok:
        return f"エラー: {reply}", False
    watch.save_config(updates)
    label = KEYS.get(key, (key, ""))[0]
    return f"{label}: {watch._fmt_cfg_value(old)} → {watch._fmt_cfg_value(new)}", True


def toggle_chain(cfg, name):
    """チェーンの ON/OFF。全部消すのは許さない（何も見なくなるので）。"""
    cur = list(cfg.get("chains") or ())
    names = [c for c in cur if c != name] if name in cur else cur + [name]
    names = [c for c in CHAIN_ORDER if c in names]
    if not names:
        return "最低 1 つは残す"
    ok, reply = watch.set_chains(cfg, names)
    return reply if ok else f"エラー: {reply}"


def text_reply(conn, cfg, text):
    """文字で来たものへの返信 (本文, ボタン)。ボタンが要らなければ None。"""
    t = text.strip()
    if t in ("/start", "/menu", "メニュー"):
        return MENU_TEXT, menu_markup()
    if t in ("チェーン", "/chains"):
        return chains_text(cfg), chains_markup(cfg)
    if t in ("しきい値", "/set"):
        return "しきい値（セクションを選ぶ）", sections_markup()
    if t in ("送信 ON/OFF", "/dryrun"):
        return dryrun_text(cfg), dryrun_markup(cfg)
    if t == "状態":
        return watch.status_text(conn, cfg), status_markup(cfg)
    if t == "近い銘柄":
        return watch.near_report(conn, cfg), refresh_markup("near")
    if t == "ヘルプ":
        return watch.HELP, None
    if t.startswith("/status"):
        return watch.dispatch_command(conn, cfg, t), status_markup(cfg)
    if t.startswith("/near"):
        return watch.dispatch_command(conn, cfg, t), refresh_markup("near")
    return watch.dispatch_command(conn, cfg, t), None


def callback_reply(conn, cfg, data):
    """ボタンへの返信 (本文, ボタン, 通知バーの短文)。本文が None なら書き換えない。"""
    kind, _, arg = data.partition(":")
    if kind == "c":
        note = toggle_chain(cfg, arg)
        return chains_text(cfg), chains_markup(cfg), note
    if kind in ("d", "sd"):
        # ボタンは「送信を」ON/OFF する。dry_run はその裏返し（dry_run=True で送らない）。
        # toggle は今の逆へ。d:on / d:off は古いメッセージのボタンのために残す
        dry = (not cfg.get("dry_run", True)) if arg == "toggle" else arg != "on"
        note = watch.set_dry_run(cfg, dry)
        if kind == "sd":         # /status の下のトグル。状態画面のまま書き換える
            return watch.status_text(conn, cfg), status_markup(cfg), note
        return dryrun_text(cfg), dryrun_markup(cfg), note
    if kind == "m":
        if arg == "chains":
            return chains_text(cfg), chains_markup(cfg), ""
        if arg == "dryrun":
            return dryrun_text(cfg), dryrun_markup(cfg), ""
        return watch.HELP, menu_markup(), ""
    if kind == "t":
        return "しきい値（セクションを選ぶ）", sections_markup(), ""
    if kind == "ts":
        title = dict(SECTIONS).get(arg, "その他")
        return f"{title}\n（押すと調整）", section_markup(cfg, arg), ""
    if kind == "tk":
        return key_text(cfg, arg), key_markup(arg), ""
    if kind == "ta":
        key, _, op = arg.rpartition(":")
        note, _changed = apply_step(cfg, key, op)
        return key_text(cfg, key), key_markup(key), note
    if kind == "r":
        if arg == "status":
            return watch.status_text(conn, cfg), status_markup(cfg), "更新した"
        return watch.near_report(conn, cfg), refresh_markup(arg), "更新した"
    return None, None, "不明なボタン"


def handle(conn, token, chat_id, update):
    """1 件ぶん。必ず何か返す（返さないと押した側は無反応に見える）。"""
    cfg = watch.load_config()
    msg = update.get("message") or {}
    cq = update.get("callback_query") or {}
    if cq:
        src = cq.get("message") or {}
        if str((src.get("chat") or {}).get("id")) != str(chat_id):
            return                   # 本人以外は黙って無視する
        # 「押した感」は何より先に返す（返さないと端末側が回り続ける）
        notify.answer_callback(token, cq.get("id", ""))
        text, buttons, note = callback_reply(conn, cfg, cq.get("data") or "")
        if text is None:
            return
        # 結果は元のメッセージの最後の行に足す（チャットを増やさずに残るように）
        if note:
            text += "\n" + note
        mid = src.get("message_id")
        # 本文が長いと editMessageText は通らないので、そのときは新しく送る
        if mid and len(text) <= notify.MAX_LEN and notify.edit_text(
                token, chat_id, mid, text, buttons):
            return
        notify.send(text, token, chat_id, buttons)
        return
    if str((msg.get("chat") or {}).get("id")) != str(chat_id):
        return                       # 本人以外は黙って無視する
    text = (msg.get("text") or "").strip()
    if not text:
        return
    reply, buttons = text_reply(conn, cfg, text)
    notify.send(reply, token, chat_id, buttons)


def run():
    conn = db.connect()
    # 自動コミット。コマンド処理の途中で例外が出ても暗黙トランザクションを
    # 開いたまま getUpdates（最長 25 秒）で待たない
    conn.isolation_level = None
    token, chat_id = notify.credentials()
    if not (token and chat_id):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が Keychain に無い。"
              "先に run.sh setup を実行する。", file=sys.stderr)
        sys.exit(2)
    notify.set_my_commands(token, COMMANDS)
    # 以前の版が出した画面下の常設キーボードを端末から片付ける（1 度だけ）
    if not rules.num(db.kv_get(conn, "kb_removed", 0)):
        notify.send("画面下の常設ボタンは廃止した。/menu でボタンを出せる。",
                    token, chat_id, notify.remove_keyboard())
        db.kv_set(conn, "kb_removed", 1)
    offset = int(rules.num(db.kv_get(conn, "tg_offset", 0)) or 0)
    print(f"[{watch.fmt_ts(time.time())}] bot start offset={offset}", flush=True)
    while True:
        updates, err = notify.get_updates(token, offset, POLL_TIMEOUT,
                                          ("message", "callback_query"))
        db.kv_set(conn, "bot_last_poll_ts", int(time.time()))
        conn.commit()
        if err:
            time.sleep(ERROR_SLEEP)
            continue
        for u in updates:
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            try:
                handle(conn, token, chat_id, u)
            except Exception as e:
                # 黙って落とさない。トークンが混ざる余地があるので必ず伏せる
                text = notify._scrub(f"エラー: {type(e).__name__}: {e}", token)
                print(f"  [bot] {text}", file=sys.stderr, flush=True)
                notify.send(text, token, chat_id)
        if updates:
            db.kv_set(conn, "tg_offset", offset)
            conn.commit()


if __name__ == "__main__":
    run()
