"""EMS の記録と通知判定（§12.4 / §12.5）。

シャドー運用の肝は「撃たなかった判定も全部残す」こと。Tomato のメモ（/mark）と
後から突き合わせて g とレベルを詰めるため、JSONL に 1 判定 1 行で書く。
SQLite の `scores` は上位だけ（Telegram の /score と予算の数えに使う）。
"""
import json
import os
import time

import db
import notify
import rules

LOG_DIR = os.path.join(db.STATE_DIR, "logs")
# snapshots はタイムラインの計算にだけ渡す（そのまま書くと 1 行が数百 KB になる）
SKIP_KEYS = ("wins", "flow_w", "flow_frac", "last_ts", "last_block", "address", "snapshots")

RISE_WINDOW = 1800    # 「上がり始め」をさかのぼって探す窓（秒）
RISE_DIP = 0.05       # この割合までの押し目は上昇が続いているとみなす
REACH_WINDOW = 3600   # 「しきい値に最初に届いた時刻」をさかのぼって探す窓（秒）


def log_path(now=None):
    return os.path.join(LOG_DIR, time.strftime("pump-score-%Y%m%d.jsonl",
                                               time.localtime(now or time.time())))


def _plain(v):
    return round(v, 4) if isinstance(v, float) else v


def entry(chain, feat, res, decision, now, extra=None):
    """JSONL / `scores.detail` に入れる 1 行ぶん。特徴量はそのまま全部残す。"""
    e = {"ts": int(now), "chain": chain, "address": feat.get("address"),
         "decision": decision}
    e.update({k: _plain(v) for k, v in feat.items() if k not in SKIP_KEYS})
    e.update({
        "score": round(rules.num(res.get("score")) or 0.0, 1),
        "level": res.get("level") or "", "reason": res.get("reason") or "",
        "c": {k: round(v, 3) for k, v in (res.get("parts") or {}).items()},
        "penalties": res.get("penalties") or [],
        "penalty": res.get("penalty") or 0.0,
        "progress": _plain(res.get("progress")),
    })
    # 名簿の人の建玉（後で「誰が入っていた銘柄が伸びたか」を突き合わせるため）
    if res.get("entries"):
        e["entries"] = [{"name": p.get("name"),
                         "buy_usd": round(rules.num(p.get("buy_usd")) or 0.0, 2),
                         "sell_usd": round(rules.num(p.get("sell_usd")) or 0.0, 2),
                         "first_buy_ts": p.get("first_buy_ts")}
                        for p in res["entries"]]
    if extra:
        e.update({k: _plain(v) for k, v in extra.items()})
    return e


def write_jsonl(row, now=None):
    """追記のみ。失敗しても判定は続ける（ログのために取引監視を止めない）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_path(now), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  [score log] {type(e).__name__}: {e}", flush=True)


def prune_logs(now=None, days=14):
    """retention_days より古い日の JSONL を消す（1 日 200MB 前後になるので放置すると膨らむ）。"""
    cutoff = time.strftime("pump-score-%Y%m%d.jsonl",
                           time.localtime((now or time.time()) - days * 86400))
    removed = 0
    try:
        for name in os.listdir(LOG_DIR):
            if name.startswith("pump-score-") and name.endswith(".jsonl") and name < cutoff:
                os.remove(os.path.join(LOG_DIR, name))
                removed += 1
    except OSError as e:
        print(f"  [score log] {type(e).__name__}: {e}", flush=True)
    return removed


def _rank(level):
    return rules.LEVELS.index(level) if level in rules.LEVELS else -1


def decide(conn, cfg, chain, address, res, state, now, can_send=True):
    """通知するか。戻りは notified / suppressed:* / below。

    昇格は即。同レベルはクールダウンと +5 点を両方満たしたときだけ。降格は黙る
    （どちらも「今は送らない」なので suppressed:cooldown にまとめる）。
    """
    sc = cfg.get("score", {})
    level = res.get("level") or ""
    value = rules.num(res.get("score")) or 0.0
    if not level or not sc.get("notify", True):
        return "below"
    last_level = (state or {}).get("last_level") or ""
    last_score = rules.num((state or {}).get("last_score")) or 0.0
    last_ts = rules.num((state or {}).get("last_notify_ts"))
    if _rank(level) <= _rank(last_level):
        if _rank(level) < _rank(last_level):
            return "suppressed:cooldown"
        cool = rules.num((sc.get("cooldown_min") or {}).get(level)) or 0
        if last_ts is not None and now - last_ts < cool * 60:
            return "suppressed:cooldown"
        if value < last_score + (rules.num(sc.get("renotify_delta")) or 0):
            return "suppressed:cooldown"
    if not can_send or cfg.get("dry_run", True):
        return "suppressed:dry_run"
    # 通数の上限は設けない（2026-09-19 Tomato: 多すぎるなら検知条件を直す。上限で隠さない）。
    # 以前の 時間 6/3/2/1・日 60/20/8/3 は、盛り上がる日に 11:35 で枯れて午後の 90 点を 57 銘柄捨てた。
    return "notified"


# --- 通知のタイムライン -----------------------------------------------------

def _int(v):
    v = rules.num(v)
    return None if v is None else int(v)


def rise_start(snapshots, now, window=RISE_WINDOW, dip=RISE_DIP):
    """今の上昇が始まった (時刻, そのときの価格)。分からなければ (None, None)。

    新しい方から 1 本ずつさかのぼり、「そこから後ろで dip を超えて下げていない」最後の
    点を探す。大きく押した所で止まるので、直前の谷が上昇開始になる。ずっと上げっぱなし
    なら窓の先頭（＝見えている範囲の最初）を返す。
    """
    rows = sorted((s for s in snapshots
                   if rules.num(s.get("price")) and rules.num(s.get("ts")) is not None
                   and now - rules.num(s["ts"]) <= window),
                  key=lambda s: s["ts"])
    if not rows:
        return None, None
    best = rows[-1]
    floor_ = rules.num(best["price"])
    for s in reversed(rows[:-1]):
        price = rules.num(s["price"])
        if price > floor_ * (1 + dip):   # ここから下げている＝上昇はこの後から
            break
        best, floor_ = s, min(floor_, price)
    return _int(best["ts"]), rules.num(best["price"])


def reach_ts(conn, chain, address, cut, now, window=REACH_WINDOW, since=None):
    """その点数に最初に届いた時刻。過去に記録が無ければ now（＝今が初到達）。

    今回の判定はまだ scores に入っていないので、無いときは now でよい。since（上昇開始）を
    渡すとそれより前の到達は数えない。前の波で届いた時刻を拾うと 上昇開始 → 到達 が
    負になって「?」になる（2026-09-19 の MANY / 以太）。
    """
    ts = db.first_score_ts(conn, chain, address, max(now - window, since or 0), cut or 0)
    return int(now if ts is None else ts)


TIMELINE_STAGES = (("created_ts", "作成"), ("first_seen_ts", "初観測"),
                   ("rise_start_ts", "上昇開始"), ("reach_ts", "到達"),
                   ("judged_ts", "判定"), ("send_start_ts", "送信"),
                   ("sent_ts", "TG受理"))


def timeline(conn, cfg, chain, feat, res, now, extra=None, sent=None):
    """alerts に入れる 1 行ぶん。銘柄の生い立ちと送信の実測を 1 本の時系列にする。"""
    extra, sent = extra or {}, sent or {}
    address = feat.get("address")
    tok = db.get_token(conn, chain, address) or {}
    level = res.get("level") or ""
    cuts = {**rules.LEVEL_CUTS, **((cfg.get("score") or {}).get("levels") or {})}
    start_ts, start_price = rise_start(extra.get("snapshots") or [], now)
    detail = {k: v for k, v in extra.items() if k != "snapshots"}
    # tokens.first_seen_ts は「初めて判定を通った時刻」なので、レーンが見た最初の取引があればそちら
    seen = [x for x in (_int(extra.get("first_seen_ts")), _int(tok.get("first_seen_ts"))) if x]
    return {
        "chain": chain, "address": address,
        "symbol": feat.get("symbol") or tok.get("symbol"),
        "lane": extra.get("lane"), "level": level,
        "score": round(rules.num(res.get("score")) or 0.0, 1),
        "created_ts": _int(extra.get("created_ts")) or _int(tok.get("creation_ts")),
        "first_seen_ts": min(seen) if seen else None,
        "rise_start_ts": start_ts, "rise_price": start_price,
        "first_score_ts": db.first_score_ts(conn, chain, address, now - REACH_WINDOW, 0),
        "reach_ts": reach_ts(conn, chain, address, rules.num(cuts.get(level)), now,
                             since=start_ts),
        "judged_ts": int(now), "judged_price": rules.num(extra.get("price")),
        "data_ts": rules.num(extra.get("data_ts")),
        "send_start_ts": rules.num(sent.get("send_start_ts")),
        "sent_ts": rules.num(sent.get("sent_ts")),
        "tg_date": _int(sent.get("tg_date")), "message_id": _int(sent.get("message_id")),
        "photo": 1 if sent.get("photo") else 0, "ok": 1 if notify.sent_ok(sent) else 0,
        "error": sent.get("error"),
        "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
    }


def timeline_body(row):
    """段の並びと結果。最初の段だけ実時刻で、あとは前の段からの経過を出す。

    欠けた段は「?」にして、次の段は分かっている最後の段から測る。
    """
    parts, prev = [], None
    for key, label in TIMELINE_STAGES:
        ts = rules.num(row.get(key))
        if prev is None:
            parts.append(f"{label} {time.strftime('%H:%M:%S', time.localtime(ts))}"
                         if ts is not None else f"{label} ?")
        else:
            parts.append(f"{label} {rules.fmt_gap(None if ts is None else ts - prev)}")
        prev = ts if ts is not None else prev
    tail = f" 画像={'○' if row.get('photo') else '×'}"
    if row.get("message_id"):
        tail += f" id={row['message_id']}"
    if row.get("error"):
        tail += f" err={row['error']}"
    return " → ".join(parts) + tail


def timeline_line(row):
    """launchd ログに出す 1 行。"""
    return (f"alert {row.get('symbol') or '?'} {row.get('level') or '?'}"
            f" {rules.num(row.get('score')) or 0:.0f} " + timeline_body(row))


def judge(conn, cfg, chain, feat, res, now, send=None, extra=None):
    """採点結果を記録し、送ると決まれば send() を呼ぶ。戻りは decision。

    send は本文の組み立てから送信までを包んだ関数。送信の実測（dict）か True/False を
    返す。None なら送らない＝ suppressed:dry_run。DB への記録は送信の成否まで見てから
    確定する。送ると決めたぶんは成否によらず alerts にも 1 行残す（届かなかったことも
    後から追えるように）。
    """
    sc = cfg.get("score", {})
    address = feat.get("address")
    state = db.get_score_state(conn, chain, address)
    decision = decide(conn, cfg, chain, address, res, state, now, can_send=send is not None)
    tl = None
    if decision == "notified":
        result = send()
        tl = timeline(conn, cfg, chain, feat, res, now, extra,
                      result if isinstance(result, dict) else {"ok": bool(result)})
        db.insert_alert(conn, tl)
        print(timeline_line(tl), flush=True)
        if notify.sent_ok(result):
            db.set_score_state(conn, chain, address, res.get("level"),
                               rules.num(res.get("score")) or 0.0, now)
        else:
            decision = "suppressed:send_failed"
    row = entry(chain, feat, res, decision, now, extra)
    if tl:
        row["timeline"] = {k: v for k, v in tl.items()
                           if k not in ("chain", "address", "symbol", "detail")}
    value = row["score"]
    changed = (res.get("level") or "") != ((state or {}).get("last_level") or "")
    if value >= (rules.num(sc.get("log_min_score")) or 30) or decision != "below" or changed:
        write_jsonl(row, now)
    if value >= (rules.num(sc.get("db_min_score")) or 40) or decision != "below":
        db.insert_score(conn, chain, address, now, {
            "symbol": feat.get("symbol"), "score": value, "level": res.get("level") or "",
            "decision": decision, "notified": decision == "notified",
            "detail": json.dumps(row, ensure_ascii=False)})
    conn.commit()
    return decision
