"""通知に足す「画像」と「安全確認」。両レーン（watch cycle / chain_watch）から使う。

- 画像: (1) DexScreener の `info.imageUrl`（毎サイクルの一括更新に入っている＝追加コール 0）、
  (2) 無ければ BSC は four.meme の公開 API、それでも無ければ DexScreener を名指しで 1 回。
  GMGN `token info` の `logo` は gmgn.ai が Cloudflare で 403 を返すため Telegram が
  取りに行けない（photo_url 参照）。URL が取れたら**本体もここで落として**キャッシュに置き
  （logo_path）、送信は multipart にする。結果は「無かった」も tokens 表に残すが
  （logo_ts / logo_tries）、生まれたては画像の登録が後からなので数回は聞き直す。
- 安全確認: GoPlus の無料 API（鍵不要）。tokens 表に 30 分キャッシュし、
  通知経路からはタイムアウト 2 秒・再試行なしで引く（遅らせるくらいなら「未取得」と出す）。

GMGN は共有バケット（sources/gmgn.BUCKET_WINDOW 秒窓）を食うので、巡回中の補完は
残量 < BACKFILL_ROOM のときだけ許す（通知経路からは GMGN を引かない）。
"""
import json
import os
import time
import urllib.request

import db
import rules
from sources import dexscreener, fourmeme, gmgn, goplus

SECURITY_TTL = 1800      # 安全確認の再取得間隔（秒）
SECURITY_TIMEOUT = 2     # 通知を待たせない上限
FOURMEME_TIMEOUT = 2     # 同上（通知経路から引くので短く切る）
LOGO_TIMEOUT = 3         # 巡回のついでに引く GMGN 補完・画像本体の取得
ALERT_LOGO_TIMEOUT = 2   # 通知の直前に画像本体を落とすときの上限

ZERO_ADDRESSES = ("0x0000000000000000000000000000000000000000",
                  "0x000000000000000000000000000000000000dead",
                  "0x0000000000000000000000000000000000000001")


def _unsupported_key(chain):
    return f"goplus_unsupported_{chain}"


def _commit(conn):
    """キャッシュを確定する。呼び出し元が BEGIN IMMEDIATE の中なら何もしない。

    高速レーンの judge_token は events の読み書きを明示トランザクションで囲むので、
    ここで commit するとその囲みを壊す（キャッシュはその囲みと一緒に確定すればよい）。
    """
    if not conn.in_transaction:
        conn.commit()


def _json_or_none(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# --- 画像 -------------------------------------------------------------------

LOGO_DIR = os.path.join(db.STATE_DIR, "logos")
LOGO_MAX_BYTES = 5_000_000   # sendPhoto の上限（10MB）より手前で切る
LOGO_TRIES = 3               # 「まだ無い」を確かめる回数の上限
LOGO_RETRY_SEC = 120         # 聞き直すまでの間隔（秒）
# 拡張子は Content-Type から決める（Telegram は multipart のファイル名と型で形式を見る）
LOGO_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/webp": ".webp", "image/gif": ".gif"}


def photo_url(url):
    """sendPhoto に渡せる URL だけ通す（渡せないものは None）。

    sendPhoto は Telegram のサーバーが URL を取りに行く方式。GMGN の画像置き場
    （gmgn.ai/external-res）は Cloudflare のチャレンジで誰からも 403 を返すので
    （2026-09-18 実測。UA・Referer を付けても 403）、渡しても必ず失敗する。
    キャッシュはそのまま残す（残しておけば「GMGN は試し済み」の印になる）。
    """
    return url if url and "gmgn.ai/" not in url else None


def logo_base_path(chain, address):
    """画像本体の置き場所（拡張子なし）。チェーンごとに分ける。"""
    return os.path.join(LOGO_DIR, chain, address)


def local_logo(tok):
    """落としてある画像本体のパス。記録があってもファイルが消えていれば None。"""
    path = (tok or {}).get("logo_path")
    return path if path and os.path.isfile(path) else None


def can_fetch_logo(tok, now):
    """もう一度画像を探しに行ってよいか（回数と間隔で決める）。

    four.meme は銘柄が作られた直後だとまだ画像を登録していないことがある。1 回きりの
    否定キャッシュだと生まれたての銘柄が永久に画像なしになるので、LOGO_RETRY_SEC 秒
    あけて LOGO_TRIES 回までは聞き直す（無限に撃たないための上限でもある）。
    """
    tries = rules.num((tok or {}).get("logo_tries")) or 0
    if tries >= LOGO_TRIES:
        return False
    ts = rules.num((tok or {}).get("logo_ts"))
    return ts is None or (now - ts) >= LOGO_RETRY_SEC


def fetch_logo_url(chain, address, timeout=FOURMEME_TIMEOUT):
    """画像 URL を探す。four.meme（BSC のみ）→ DexScreener の順。無ければ None。

    DexScreener は毎サイクルの一括更新に画像が入っているが、生まれたての銘柄は
    まだ巡回対象に入っていないので、ここで名指しで 1 件だけ引く（チェーン不問）。
    """
    url = fourmeme.image(address, timeout=timeout) if chain == "bsc" else None
    if url:
        return url
    rows, _status = dexscreener.fetch(chain, [address], timeout=timeout)
    for row in rows:
        if row.get("logo"):
            return row["logo"]
    return None


def fetch_logo_file(url, path, timeout=LOGO_TIMEOUT, max_bytes=LOGO_MAX_BYTES):
    """画像本体を落として置く。置けたパスを返す（駄目なら None）。

    拡張子は Content-Type から決めるので path は拡張子なしで渡す。URL は外部データなので
    http(s) 以外は開かない（file:// を掴まされないため）。
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "pump-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = r.read(max_bytes + 1)   # 上限を 1 バイト超えたら捨てる
    except Exception:
        return None
    if not data or len(data) > max_bytes:
        return None
    dest = path + LOGO_EXT.get(mime, ".png")
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
    except OSError:
        return None
    return dest


def store_logo_file(conn, chain, address, url, timeout=LOGO_TIMEOUT):
    """画像本体を落として置き場所を tokens に記録する。落とせたパス（駄目なら None）。"""
    path = fetch_logo_file(url, logo_base_path(chain, address), timeout=timeout)
    if path:
        db.save_logo_path(conn, chain, address, path)
    return path


def logo(conn, chain, address, fetch=False, now=None, timeout=LOGO_TIMEOUT):
    """通知に付ける画像。手元の本体があればそのパス、無ければ URL（どちらも無ければ None）。

    パスを返したときは multipart で本体を送れる。URL を渡して Telegram に取りに行かせると
    取りこぼしが多い（2026-09-19 の chain ログで画像が付いたのは 1148 件中 311 件）。
    fetch=False ならキャッシュを見るだけで取りに行かない。
    """
    tok = db.get_token(conn, chain, address)
    path = local_logo(tok)
    if path:
        return path
    url = photo_url((tok or {}).get("logo"))
    if not fetch or tok is None:
        return url
    now = now or time.time()
    if not url and can_fetch_logo(tok, now):
        url = photo_url(fetch_logo_url(chain, address, timeout=FOURMEME_TIMEOUT))
        db.save_logo(conn, chain, address, url, now)
    path = store_logo_file(conn, chain, address, url, timeout=timeout) if url else None
    _commit(conn)
    return path or url


def prune_logo_files(now, days=14):
    """古い画像本体を消す（消えても logo_path は URL に落ちるだけ）。消した件数を返す。"""
    cutoff = now - days * 86400
    removed = 0
    for root, _dirs, files in os.walk(LOGO_DIR):
        for name in files:
            target = os.path.join(root, name)
            try:
                if os.path.getmtime(target) < cutoff:
                    os.remove(target)
                    removed += 1
            except OSError:
                pass
    return removed


def backfill_logos(conn, chains, now=None, limit=1):
    """巡回のついでに logo キャッシュを埋める。枠に余裕があるときだけ 1 件。"""
    now = int(now or time.time())
    if not gmgn.has_room(conn, gmgn.BACKFILL_ROOM, now):
        return 0
    todo = db.tokens_without_logo(conn, list(chains), now - 3600, limit)
    filled = 0
    for chain, address in todo:
        info = gmgn.token_info(conn, chain, address, timeout=LOGO_TIMEOUT)
        db.save_gmgn_info(conn, chain, address, (info or {}).get("logo") or None,
                          json.dumps(info) if info else None, now)
        filled += 1 if (info or {}).get("logo") else 0
    _commit(conn)
    return filled


# --- 安全確認 ----------------------------------------------------------------

def cached_security(conn, chain, address):
    """GoPlus の結果のキャッシュだけ返す（無ければ None。取りに行かない＝通知経路用）。"""
    tok = db.get_token(conn, chain, address)
    return _json_or_none(tok.get("security_json")) if tok else None


def security(conn, chain, address, now=None, ttl=SECURITY_TTL):
    """GoPlus の結果（dict）。キャッシュが新しければそれ、古ければ引き直す。取れなければ None。

    チェーンごと 1 回だけ試して未対応と分かったら kv に印を付け、以後は呼ばない。
    """
    now = int(now or time.time())
    if db.kv_get(conn, _unsupported_key(chain)):
        return None
    tok = db.get_token(conn, chain, address)
    cached = _json_or_none(tok.get("security_json")) if tok else None
    ts = rules.num(tok.get("security_ts")) if tok else None
    if cached and ts and now - ts < ttl:
        return cached
    data, state = goplus.fetch(chain, address, timeout=SECURITY_TIMEOUT)
    if state == "unsupported":
        # GoPlus は「この銘柄を知らない」ときも result が空になる（チェーン未対応と同じ形）。
        # そのチェーンで一度でも取れているなら銘柄側の問題なので、チェーン単位では止めない。
        # 2026-09-18: 先回り取得が知らない銘柄を 1 件聞いた瞬間に BSC 全体が止まった。
        if not db.has_security(conn, chain):
            db.kv_set(conn, _unsupported_key(chain), now)
            _commit(conn)
        return cached
    if state != "ok":
        return cached            # 一時的な失敗なら古い値でしのぐ
    if tok is not None:
        db.save_security(conn, chain, address, json.dumps(data), now)
        _commit(conn)
    return data


# --- 表示 --------------------------------------------------------------------

def _pct(v, digits=0):
    v = rules.num(v)
    return None if v is None else f"{v * 100:.{digits}f}%"


def _flag(sec, key):
    return str(sec.get(key, "")).strip() == "1"


def _lp_locked_pct(sec):
    """LP のうち焼却（dead/ゼロ宛）またはロック済みの割合。lp_holders が無ければ None。"""
    holders = sec.get("lp_holders")
    if not isinstance(holders, list) or not holders:
        return None
    total = 0.0
    for h in holders:
        if not isinstance(h, dict):
            continue
        addr = str(h.get("address") or "").lower()
        locked = str(h.get("is_locked", "")).strip() in ("1", "True", "true")
        if locked or addr in ZERO_ADDRESSES:
            total += rules.num(h.get("percent")) or 0.0
    return total * 100


# 危険フラグ（GoPlus のキー → 表示名）。売り税 ≥10% だけは別途しきい値で見る。
DANGERS = (
    ("is_honeypot", "honeypot"),
    ("is_mintable", "ミント可"),
    ("hidden_owner", "隠れオーナー"),
    ("can_take_back_ownership", "オーナー奪還可"),
    ("transfer_pausable", "送金停止可"),
    ("is_blacklisted", "ブラックリスト有"),
    ("cannot_sell_all", "全量売却不可"),
    ("slippage_modifiable", "スリッページ可変"),
)
HIGH_SELL_TAX = 0.10


def security_parts(sec):
    """「安全:」行の要素。sec が None なら ["未取得"]。"""
    if not sec:
        return ["未取得"]
    parts = ["honeypot " + ("あり" if _flag(sec, "is_honeypot") else "なし")]
    buy, sell = rules.num(sec.get("buy_tax")), rules.num(sec.get("sell_tax"))
    if buy is not None or sell is not None:
        parts.append(f"税 買{_pct(buy) or '?'}/売{_pct(sell) or '?'}")
    owner = str(sec.get("owner_address") or "").lower()
    if owner:
        parts.append("オーナー放棄" if owner in ZERO_ADDRESSES else "オーナーあり")
    lp = _lp_locked_pct(sec)
    if lp is not None:
        parts.append(f"LP 焼却/ロック {lp:.0f}%")
    return parts


def danger_parts(sec):
    """「注意:」行の要素。無ければ空リスト（行ごと省く）。"""
    if not sec:
        return []
    out = [label for key, label in DANGERS if _flag(sec, key)]
    sell = rules.num(sec.get("sell_tax"))
    if sell is not None and sell >= HIGH_SELL_TAX:
        out.append(f"売り税 {sell * 100:.0f}%")
    return out


def holder_count(sec):
    return rules.num((sec or {}).get("holder_count"))
