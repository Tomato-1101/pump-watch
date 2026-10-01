"""pump-watch の状態 DB（SQLite / state.db）。

銘柄の初認識価格とピークを覚えておかないと「初認識比 5.7x」も「各段 1 回だけ通知」も
成立しないので、巡回のたびに tokens / snapshots / events を書き足していく。
"""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "state.db")
# 画像キャッシュと採点ログの置き場。OS 固有の場所に決め打ちせず環境変数で差し替えられるようにする
STATE_DIR = os.environ.get("PUMPWATCH_STATE_DIR") or os.path.expanduser("~/.local/state/pump-watch")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  symbol TEXT,
  name TEXT,
  launchpad TEXT,
  creation_ts INTEGER,
  first_seen_ts INTEGER,
  first_price REAL,
  first_mcap REAL,
  peak_price REAL,
  peak_mcap REAL,
  peak_ts INTEGER,
  last_price REAL,
  last_mcap REAL,
  last_seen_ts INTEGER,
  last_active_ts INTEGER,
  logo TEXT,
  gmgn_info_ts INTEGER,
  gmgn_info_json TEXT,
  security_json TEXT,
  security_ts INTEGER,
  PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS snapshots (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  ts INTEGER NOT NULL,
  price REAL,
  mcap REAL,
  buys5m INTEGER,
  sells5m INTEGER,
  buyers5m INTEGER,
  holders INTEGER,
  smart INTEGER,
  renowned INTEGER,
  liquidity REAL,
  vol5m REAL,
  vol24h REAL,
  rank5m INTEGER,
  rank24h INTEGER,
  hot_level INTEGER,
  visiting INTEGER,
  wash INTEGER,
  pc5m REAL,
  pc1h REAL,
  pc24h REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_token ON snapshots (chain, address, ts);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  "trigger" TEXT NOT NULL,
  stage TEXT,
  value REAL,
  heat INTEGER,
  yabai INTEGER,
  text TEXT,
  message TEXT,
  notified INTEGER DEFAULT 0,
  pending INTEGER DEFAULT 0,
  batched_ts INTEGER,
  sent INTEGER DEFAULT 0,
  sent_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_token ON events (chain, address, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS api_calls (
  ts INTEGER NOT NULL,
  cmd TEXT,
  ok INTEGER,
  retries INTEGER
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  symbol TEXT,
  score REAL,
  level TEXT,
  decision TEXT,
  notified INTEGER DEFAULT 0,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_ts ON scores (ts);
CREATE INDEX IF NOT EXISTS idx_scores_token ON scores (chain, address, ts);
CREATE TABLE IF NOT EXISTS score_state (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  last_level TEXT,
  last_score REAL,
  last_notify_ts INTEGER,
  PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS wallet_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  wallet TEXT NOT NULL,
  address TEXT NOT NULL,
  ts INTEGER NOT NULL,
  side TEXT,
  usd REAL,
  price REAL,
  block INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_token ON wallet_trades (chain, address, ts);
CREATE TABLE IF NOT EXISTS marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  memo TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_marks_ts ON marks (ts);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  symbol TEXT,
  lane TEXT,
  level TEXT,
  score REAL,
  created_ts INTEGER,
  first_seen_ts INTEGER,
  rise_start_ts INTEGER,
  rise_price REAL,
  first_score_ts INTEGER,
  reach_ts INTEGER,
  judged_ts INTEGER,
  judged_price REAL,
  data_ts REAL,
  send_start_ts REAL,
  sent_ts REAL,
  tg_date INTEGER,
  message_id INTEGER,
  photo INTEGER,
  ok INTEGER,
  error TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_token ON alerts (chain, address, judged_ts);
"""

# 盛り上がり確定レーン（hot.py）の 3 表。既存レーンは触らないので SCHEMA とは分けて
# _migrate から作る（古い state.db にも後から生える）。2026-09-20
HOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS hot_obs (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  ts INTEGER NOT NULL,
  src TEXT,
  symbol TEXT,
  swaps INTEGER,
  buys INTEGER,
  sells INTEGER,
  volume REAL,
  holders INTEGER,
  price REAL,
  market_cap REAL,
  liquidity REAL,
  chg24 REAL,
  smart_count INTEGER,
  kol_count INTEGER,
  hot_level INTEGER,
  wash INTEGER,
  honeypot INTEGER,
  bot_rate REAL,
  bundler_rate REAL,
  rat_rate REAL,
  launchpad TEXT,
  open_ts INTEGER,
  total_supply REAL,
  ath_mcap REAL,
  twitter TEXT,
  PRIMARY KEY (chain, address, ts)
);
CREATE INDEX IF NOT EXISTS idx_hot_obs_ts ON hot_obs (ts);
CREATE TABLE IF NOT EXISTS hot_state (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  streak INTEGER DEFAULT 0,
  last_pass_ts INTEGER,
  last_notified_ts INTEGER,
  PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS hot_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  day_jst TEXT NOT NULL,
  symbol TEXT,
  confirmed_ts INTEGER,
  notified_ts INTEGER,
  sent INTEGER DEFAULT 0,
  message TEXT,
  row_json TEXT,
  wallets_json TEXT,
  UNIQUE (chain, address, day_jst)
);
CREATE INDEX IF NOT EXISTS idx_hot_hits_day ON hot_hits (day_jst);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL では NORMAL でもアプリの異常終了に対しては安全（失うのは OS 落ち/停電時の直前分だけ）。
    # commit ごとの fsync が無くなり、書き込みロックを握る時間が短くなる（3 プロセスで共有するため）
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """既存 state.db を捨てずに列を追随させる（初認識価格とピークを失わないため）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "rank1h" in cols and "rank24h" not in cols:
        # 1h trending の取得をやめ 24h を毎サイクル取るようにしたため（2026-09-18）
        conn.execute("ALTER TABLE snapshots RENAME COLUMN rank1h TO rank24h")
    if "vol24h" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN vol24h REAL")
    if "vol5m" not in cols:
        # S2（出来高急増）は 5 分出来高の平常比で見るため（2026-09-18）
        conn.execute("ALTER TABLE snapshots ADD COLUMN vol5m REAL")
    if "buyers5m" not in cols:
        # GeckoTerminal は買い手の実数を返す（S3 は回数より実数を優先する。2026-09-18）
        conn.execute("ALTER TABLE snapshots ADD COLUMN buyers5m INTEGER")
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
    if "last_active_ts" not in tcols:
        # 自分の一括更新で last_seen_ts が伸び続けると更新対象が減らないので、
        # 「取引があった時刻」を別に持つ（2026-09-18）
        conn.execute("ALTER TABLE tokens ADD COLUMN last_active_ts INTEGER")
        conn.execute("UPDATE tokens SET last_active_ts=last_seen_ts")
    if "logo" not in tcols:
        # 通知に画像を付ける。GMGN の token info は共有バケットを消費するので、
        # 一度取れた logo は銘柄ごとに永続キャッシュする（2026-09-18）
        conn.execute("ALTER TABLE tokens ADD COLUMN logo TEXT")
        conn.execute("ALTER TABLE tokens ADD COLUMN gmgn_info_ts INTEGER")
        conn.execute("ALTER TABLE tokens ADD COLUMN gmgn_info_json TEXT")
    if "security_json" not in tcols:
        # GoPlus の安全確認は 30 分キャッシュする（通知のたびに叩かない。2026-09-18）
        conn.execute("ALTER TABLE tokens ADD COLUMN security_json TEXT")
        conn.execute("ALTER TABLE tokens ADD COLUMN security_ts INTEGER")
    if "logo_ts" not in tcols:
        # 画像を取りに行った印。画像が無かった銘柄にも二度撃たないため、
        # 「取れなかった」もキャッシュする（2026-09-18）
        conn.execute("ALTER TABLE tokens ADD COLUMN logo_ts INTEGER")
    if "logo_path" not in tcols:
        # URL を渡す sendPhoto は Telegram 側の取得で落ちる（2026-09-19 の chain ログで
        # 画像が用意できたのは 1148 件中 311 件）。本体を手元に落として multipart で送る。
        # logo_tries は「無かった」を何回確かめたか（four.meme は作成直後だとまだ画像が無い）
        conn.execute("ALTER TABLE tokens ADD COLUMN logo_path TEXT")
        conn.execute("ALTER TABLE tokens ADD COLUMN logo_tries INTEGER DEFAULT 0")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(api_calls)")}
    if "n" not in acols:
        # 1 行が何コールぶんか。DexScreener は 1 サイクル 100 コール超をまとめて
        # 1 行で記録するので、60 秒の予算は行数ではなく回数で数える（2026-09-18）
        conn.execute("ALTER TABLE api_calls ADD COLUMN n INTEGER DEFAULT 1")
        conn.execute("UPDATE api_calls SET n=1 WHERE n IS NULL")
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "notified" not in ecols:
        # 1 サイクル 1 銘柄で通知は 1 通なので、本文を持つ行は代表 1 行だけになった。
        # 「発火した」ことは message ではなく notified で数える（2026-09-18）。
        conn.execute("ALTER TABLE events ADD COLUMN notified INTEGER DEFAULT 0")
        conn.execute('UPDATE events SET notified=1 WHERE message<>""')
    if "pending" not in ecols:
        # 2 段配信（即時 / まとめ）。pending=1 は「まとめ待ち」、
        # batched_ts はまとめに載せた時刻（1 回のまとめ = 同じ batched_ts）。2026-09-18
        conn.execute("ALTER TABLE events ADD COLUMN text TEXT")
        conn.execute("ALTER TABLE events ADD COLUMN pending INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE events ADD COLUMN batched_ts INTEGER")
    conn.executescript(HOT_SCHEMA)   # 盛り上がり確定レーン（hot.py）の表


# --- kv -------------------------------------------------------------------

def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn, key, value):
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --- tokens ---------------------------------------------------------------

def upsert_token(conn, chain, address, row, now, active=True):
    """銘柄行を作成/更新して最新の dict を返す。first_* は初認識時の値を保持する。

    active=False（5 分間まったく取引が無い）のときは last_active_ts を伸ばさない。
    これが伸び続けると DexScreener の一括更新の対象が永久に増える。

    first_* がまだ空の行（ensure_token が作った最小行 / 価格の取れない初回）には、
    価格が付いた最初のここで入れる。空のままだと M2 の倍率（price / first_price）が
    永久に計算できない。埋まっている値は COALESCE で触らない。
    """
    cur = conn.execute(
        "SELECT * FROM tokens WHERE chain=? AND address=?", (chain, address)
    ).fetchone()
    price = row.get("price")
    mcap = row.get("market_cap")
    if cur is None:
        conn.execute(
            """INSERT INTO tokens(chain,address,symbol,name,launchpad,creation_ts,
                  first_seen_ts,first_price,first_mcap,peak_price,peak_mcap,peak_ts,
                  last_price,last_mcap,last_seen_ts,last_active_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chain, address, row.get("symbol"), row.get("name"), row.get("launchpad"),
             row.get("creation_ts"), now, price, mcap, price, mcap, now, price, mcap, now, now),
        )
    else:
        peak_price = cur["peak_price"]
        peak_mcap = cur["peak_mcap"]
        peak_ts = cur["peak_ts"]
        if price is not None and (peak_price is None or price > peak_price):
            peak_price, peak_ts = price, now
        if mcap is not None and (peak_mcap is None or mcap > peak_mcap):
            peak_mcap, peak_ts = mcap, now
        conn.execute(
            """UPDATE tokens SET symbol=COALESCE(?,symbol), name=COALESCE(?,name),
                  launchpad=COALESCE(?,launchpad), creation_ts=COALESCE(?,creation_ts),
                  first_seen_ts=COALESCE(first_seen_ts,?), first_price=COALESCE(first_price,?),
                  first_mcap=COALESCE(first_mcap,?),
                  peak_price=?, peak_mcap=?, peak_ts=?,
                  last_price=COALESCE(?,last_price), last_mcap=COALESCE(?,last_mcap),
                  last_seen_ts=?, last_active_ts=COALESCE(?,last_active_ts)
               WHERE chain=? AND address=?""",
            (row.get("symbol"), row.get("name"), row.get("launchpad"), row.get("creation_ts"),
             now, price, mcap,
             peak_price, peak_mcap, peak_ts, price, mcap, now, now if active else None,
             chain, address),
        )
    r = conn.execute(
        "SELECT * FROM tokens WHERE chain=? AND address=?", (chain, address)
    ).fetchone()
    return dict(r)


def ensure_token(conn, chain, address, symbol=None, launchpad=None, creation_ts=None):
    """銘柄行が無ければ名札だけの行を作る（あれば何もしない）。作ったら True。

    先回り取得が生まれたての銘柄の画像を掴んでも、save_logo は UPDATE なので行が無いと
    捨ててしまう。ここで作るのは symbol / launchpad / creation_ts だけ。価格・時価総額・
    first_*（M2 の倍率と「初認識」の基準）・last_*（更新対象の判定）は一切書かない。
    最初の実価格は judge_token の upsert_token が入れる。
    """
    if get_token(conn, chain, address) is not None:
        return False
    # 判定スレッドが同じ瞬間に upsert_token で行を作ることがある（先回り取得と競合）ので
    # OR IGNORE。UNIQUE 違反で落とすと呼び出し側のトランザクションが開いたまま残る
    cur = conn.execute(
        "INSERT OR IGNORE INTO tokens(chain,address,symbol,launchpad,creation_ts)"
        " VALUES(?,?,?,?,?)",
        (chain, address, symbol, launchpad, int(creation_ts) if creation_ts else None))
    return cur.rowcount > 0


def get_token(conn, chain, address):
    r = conn.execute("SELECT * FROM tokens WHERE chain=? AND address=?",
                     (chain, address)).fetchone()
    return dict(r) if r else None


def save_gmgn_info(conn, chain, address, logo, payload, ts):
    """通知に付ける画像（logo）と生 JSON をキャッシュする。取れなかった logo は上書きしない。"""
    conn.execute(
        "UPDATE tokens SET logo=COALESCE(?,logo), gmgn_info_ts=?, gmgn_info_json=?"
        " WHERE chain=? AND address=?",
        (logo, int(ts), payload, chain, address))


def save_logo(conn, chain, address, url, ts=None):
    """通知に使える画像 URL を入れる（使えない URL を掴んでいたら差し替える）。

    ts を渡すと「取りに行った」印（logo_ts）も付く。url=None で ts だけ渡せば
    「探したが無かった」の記録になり、logo_tries が 1 増える（enrich 側が回数と
    間隔で再取得の可否を決める。1 回の空振りで永久に諦めないため）。
    """
    conn.execute(
        "UPDATE tokens SET logo=COALESCE(?,logo), logo_ts=COALESCE(?,logo_ts),"
        " logo_tries=COALESCE(logo_tries,0)+? WHERE chain=? AND address=?",
        (url, int(ts) if ts else None, 1 if (ts and not url) else 0, chain, address))


def save_logo_path(conn, chain, address, path):
    """落とした画像本体の置き場所。multipart で送るときはここを見る。"""
    conn.execute("UPDATE tokens SET logo_path=? WHERE chain=? AND address=?",
                 (path, chain, address))


def save_security(conn, chain, address, payload, ts):
    conn.execute("UPDATE tokens SET security_json=?, security_ts=? WHERE chain=? AND address=?",
                 (payload, int(ts), chain, address))


def has_security(conn, chain):
    """そのチェーンで GoPlus の結果を一度でも保存できたか（チェーン単位の未対応判定に使う）。"""
    return conn.execute("SELECT 1 FROM tokens WHERE chain=? AND security_ts IS NOT NULL LIMIT 1",
                        (chain,)).fetchone() is not None


def tokens_without_logo(conn, chains, since_ts, limit=1):
    """直近に取引があったのに logo が無い銘柄（バックグラウンド補完の対象）。"""
    if not chains:
        return []
    q = ",".join("?" * len(chains))
    return [(r["chain"], r["address"]) for r in conn.execute(
        f"SELECT chain,address FROM tokens WHERE logo IS NULL AND chain IN ({q})"
        " AND COALESCE(last_active_ts,last_seen_ts)>=?"
        " AND COALESCE(gmgn_info_ts,0)=0 ORDER BY last_seen_ts DESC LIMIT ?",
        (*chains, int(since_ts), int(limit)))]


SNAPSHOT_COLS = ("price", "mcap", "buys5m", "sells5m", "buyers5m", "holders", "smart",
                 "renowned", "liquidity", "vol5m", "vol24h", "rank5m", "rank24h", "hot_level",
                 "visiting", "wash", "pc5m", "pc1h", "pc24h")


def insert_snapshot(conn, chain, address, ts, snap):
    conn.execute(
        "INSERT INTO snapshots(chain,address,ts,%s) VALUES(?,?,?,%s)"
        % (",".join(SNAPSHOT_COLS), ",".join("?" * len(SNAPSHOT_COLS))),
        (chain, address, ts, *(snap.get(c) for c in SNAPSHOT_COLS)),
    )


def _addresses(conn, since_ts, chains, order, limit=0):
    where, args = ["COALESCE(last_active_ts,last_seen_ts)>=?"], [int(since_ts)]
    if chains:
        where.append(f"chain IN ({','.join('?' * len(chains))})")
        args.extend(chains)
    sql = f"SELECT chain,address FROM tokens WHERE {' AND '.join(where)} ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [(r["chain"], r["address"]) for r in conn.execute(sql, args)]


def active_addresses(conn, since_ts, chains=None, limit=0):
    """since_ts 以降に取引があった銘柄を [(chain, address), ...] で返す（DS の一括更新用）。

    活動が新しい順。1 分の予算に収まらないぶんは後ろから落とすので並びに意味がある。
    """
    return _addresses(conn, since_ts, chains,
                      "COALESCE(last_active_ts,last_seen_ts) DESC", limit)


def stale_addresses(conn, since_ts, chains=None, limit=0):
    """同じ母集団を「最終更新が古い順」で返す（予算の一部をここに回す）。

    活動順だけで切ると、静かな銘柄が永久に更新されないまま取り残される。
    """
    return _addresses(conn, since_ts, chains, "last_seen_ts ASC", limit)


def prune_snapshots(conn, now, days):
    conn.execute("DELETE FROM snapshots WHERE ts < ?", (int(now - days * 86400),))


# --- history / events -----------------------------------------------------

def get_history(conn, chain, address, now, snapshot_hours=3):
    """rules.evaluate に渡す履歴。現在のスナップショットを入れる前に呼ぶこと。"""
    # stage は「消費した段」なので通知しなかった下段マークも数える。
    # last_ts（クールダウン）と recent（熱度）は実際に発火した分だけ数える。
    fired = {}
    for r in conn.execute(
        'SELECT "trigger", stage, ts, notified FROM events WHERE chain=? AND address=?',
        (chain, address),
    ):
        d = fired.setdefault(r["trigger"], {"stages": set(), "last_ts": None})
        if r["stage"]:
            d["stages"].add(str(r["stage"]))
        if r["notified"] and (d["last_ts"] is None or r["ts"] > d["last_ts"]):
            d["last_ts"] = r["ts"]
    recent = {
        r["trigger"]
        for r in conn.execute(
            'SELECT DISTINCT "trigger" FROM events WHERE chain=? AND address=? AND ts>=?'
            ' AND notified=1',
            (chain, address, int(now - 86400)),
        )
    }
    snaps = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM snapshots WHERE chain=? AND address=? AND ts<? AND ts>=? ORDER BY ts ASC",
            (chain, address, int(now), int(now - snapshot_hours * 3600)),
        )
    ]
    return {"fired": fired, "recent_triggers": recent, "snapshots": snaps}


def insert_event(conn, chain, address, ts, ev, message, sent, pending=0):
    """発火 1 件 = 1 行。message は 1 銘柄 1 サイクルにつき代表 1 行だけが持つ。

    pending=1 はまとめ送信待ち（message は空のまま、text がまとめの 1 行になる）。
    """
    conn.execute(
        'INSERT INTO events(ts,chain,address,"trigger",stage,value,heat,yabai,text,message,'
        "notified,pending,sent,sent_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts), chain, address, ev.trigger, ev.stage, ev.value, ev.heat,
         1 if ev.yabai else 0, ev.text, message, 1 if ev.notify else 0,
         1 if pending else 0, 1 if sent else 0, int(ts) if sent else None),
    )


def last_immediate_ts(conn, chain, address):
    """その銘柄を最後に即時通知した時刻（無ければ None）。即時のクールダウン用。"""
    r = conn.execute('SELECT MAX(ts) AS ts FROM events WHERE chain=? AND address=?'
                     ' AND message<>""', (chain, address)).fetchone()
    return r["ts"] if r and r["ts"] is not None else None


def pending_events(conn):
    """まとめ待ちのイベント（古い順）。"""
    return [dict(r) for r in conn.execute(
        "SELECT id,ts,chain,address,\"trigger\",text,yabai FROM events"
        " WHERE pending=1 ORDER BY ts ASC")]


def mark_batched(conn, ids, ts, sent):
    """まとめに載せた印。1 回のまとめ送信で同じ batched_ts が付く。"""
    if not ids:
        return
    q = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE events SET pending=0, batched_ts=?, sent=?, sent_ts=? WHERE id IN ({q})",
        (int(ts), 1 if sent else 0, int(ts) if sent else None, *ids))


def latest(conn, chain, address):
    """まとめの 1 行を組み立てるための最新値（tokens + 直近スナップショット）。"""
    t = conn.execute("SELECT * FROM tokens WHERE chain=? AND address=?",
                     (chain, address)).fetchone()
    s = conn.execute("SELECT * FROM snapshots WHERE chain=? AND address=?"
                     " ORDER BY ts DESC LIMIT 1", (chain, address)).fetchone()
    out = dict(t) if t else {}
    if s:
        out.update({k: v for k, v in dict(s).items() if v is not None})
    return out


def record_api(conn, ts, cmd, ok, retries, n=1):
    """API を叩いたことを記録する。n は 1 行がまとめている実コール数。"""
    conn.execute("INSERT INTO api_calls(ts,cmd,ok,retries,n) VALUES(?,?,?,?,?)",
                 (int(ts), cmd, 1 if ok else 0, int(retries), int(n)))


def api_calls_since(conn, since_ts, prefix):
    """since_ts 以降に cmd が prefix で始まる呼び出しを何回したか（移動窓の予算判定）。

    API レーンと高速レーンは別プロセスなので、枠の共有はこの表を数えるしかない。
    1 行が複数コールぶんのことがある（DexScreener の一括更新）ので n を足す。
    """
    r = conn.execute("SELECT COALESCE(SUM(COALESCE(n,1)),0) AS n FROM api_calls"
                     " WHERE ts>=? AND cmd LIKE ?",
                     (int(since_ts), prefix.replace("%", "") + "%")).fetchone()
    return int(r["n"] or 0)


def last_api_ts(conn, prefix):
    """prefix で始まる呼び出しの最終時刻。一度も撃っていなければ None。"""
    r = conn.execute("SELECT MAX(ts) AS ts FROM api_calls WHERE cmd LIKE ?",
                     (prefix.replace("%", "") + "%",)).fetchone()
    return None if r["ts"] is None else int(r["ts"])


def trigger_counts(conn, since):
    """since 以降に発火したイベントをトリガー別に数える（段の消費マークは数えない）。"""
    return {
        r["trigger"]: r["n"]
        for r in conn.execute(
            'SELECT "trigger", COUNT(*) AS n FROM events WHERE ts>=? AND notified=1'
            ' GROUP BY "trigger" ORDER BY "trigger"',
            (int(since),),
        )
    }


def message_count(conn, since):
    """since 以降に組み立てた通知の通数（複数トリガーが同時に出ても 1 通）。"""
    r = conn.execute('SELECT COUNT(*) AS n FROM events WHERE ts>=? AND message<>""',
                     (int(since),)).fetchone()
    return int(r["n"] or 0)


def batch_counts(conn, since):
    """since 以降のまとめ送信 (通数, 件数)。1 回のまとめ = 1 つの batched_ts。"""
    r = conn.execute(
        "SELECT COUNT(DISTINCT batched_ts) AS n, COUNT(*) AS items FROM events"
        " WHERE batched_ts>=?", (int(since),)).fetchone()
    return int(r["n"] or 0), int(r["items"] or 0)


def retry_count(conn, since):
    r = conn.execute("SELECT COALESCE(SUM(retries),0) AS n FROM api_calls WHERE ts>=?",
                     (int(since),)).fetchone()
    return int(r["n"] or 0)


# --- EMS スコア（シャドー運用）---------------------------------------------

def insert_score(conn, chain, address, ts, row):
    """1 判定ぶんの採点結果。detail は JSON 文字列（内訳・特徴量をそのまま）。"""
    conn.execute(
        "INSERT INTO scores(ts,chain,address,symbol,score,level,decision,notified,detail)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (int(ts), chain, address, row.get("symbol"), float(row.get("score") or 0),
         row.get("level") or "", row.get("decision") or "",
         1 if row.get("notified") else 0, row.get("detail")))


def get_score_state(conn, chain, address):
    """その銘柄の最後の通知（レベル・点数・時刻）。無ければ None。"""
    r = conn.execute("SELECT * FROM score_state WHERE chain=? AND address=?",
                     (chain, address)).fetchone()
    return dict(r) if r else None


def set_score_state(conn, chain, address, level, score, ts):
    conn.execute(
        "INSERT INTO score_state(chain,address,last_level,last_score,last_notify_ts)"
        " VALUES(?,?,?,?,?) ON CONFLICT(chain,address) DO UPDATE SET"
        " last_level=excluded.last_level, last_score=excluded.last_score,"
        " last_notify_ts=excluded.last_notify_ts",
        (chain, address, level, float(score), int(ts)))



def top_scores(conn, since, limit=10):
    """since 以降の銘柄別の最高点（高い順）。/score の一覧に使う。"""
    return [dict(r) for r in conn.execute(
        "SELECT chain,address,MAX(score) AS score, MAX(ts) AS ts,"
        " MAX(symbol) AS symbol FROM scores WHERE ts>=?"
        " GROUP BY chain,address ORDER BY score DESC LIMIT ?", (int(since), int(limit)))]


def first_score_ts(conn, chain, address, since, minimum):
    """since 以降にその銘柄が minimum 点以上を出した最初の時刻（無ければ None）。

    タイムラインの「上がり始めてから何分でしきい値に届いたか」を出すのに使う。
    """
    minimum = float(minimum)
    r = conn.execute(
        "SELECT MIN(ts) AS ts FROM scores WHERE chain=? AND address=? AND ts>=? AND score>=?",
        (chain, address, int(since), minimum)).fetchone()
    return None if r["ts"] is None else int(r["ts"])


def prune_scores(conn, now, days=14):
    conn.execute("DELETE FROM scores WHERE ts < ?", (int(now - days * 86400),))


# --- 通知のタイムライン -----------------------------------------------------

ALERT_COLS = ("chain", "address", "symbol", "lane", "level", "score",
              "created_ts", "first_seen_ts", "rise_start_ts", "rise_price",
              "first_score_ts", "reach_ts", "judged_ts", "judged_price", "data_ts",
              "send_start_ts", "sent_ts", "tg_date", "message_id", "photo", "ok",
              "error", "detail")


def insert_alert(conn, row):
    """通知 1 通ぶんの時系列（作成 → 初観測 → 上昇開始 → 到達 → 判定 → 送信 → TG 受理）。"""
    conn.execute(
        "INSERT INTO alerts(%s) VALUES(%s)"
        % (",".join(ALERT_COLS), ",".join("?" * len(ALERT_COLS))),
        tuple(row.get(c) for c in ALERT_COLS))


def recent_alerts(conn, chain, address, limit=5):
    """その銘柄の通知記録を新しい順に。chain=None なら全チェーンから拾う。"""
    where, args = ["address=?"], [address]
    if chain:
        where.append("chain=?")
        args.append(chain)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM alerts WHERE {' AND '.join(where)}"
        " ORDER BY judged_ts DESC, id DESC LIMIT ?", (*args, int(limit)))]


def prune_alerts(conn, now, days=14):
    conn.execute("DELETE FROM alerts WHERE judged_ts < ?", (int(now - days * 86400),))


# --- 名簿ウォレットの取引 ---------------------------------------------------

def insert_wallet_trades(conn, chain, rows):
    """名簿ウォレットの生取引をまとめて入れる。

    rows は (wallet, address, ts, side, usd, price, block)。executemany 1 回で書く
    （判定スレッドの conn は autocommit なので、ここでロックを握り続けない）。
    """
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO wallet_trades(chain,wallet,address,ts,side,usd,price,block)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(chain, str(w).lower(), a, int(ts), side, float(usd or 0), float(price or 0),
          int(block or 0)) for w, a, ts, side, usd, price, block in rows])
    return len(rows)


def wallet_entries(conn, chain, address, since):
    """その銘柄の名簿ウォレットの取引を wallet 単位でまとめる（買い額の大きい順）。

    buy_qty は price > 0 の買いだけを Σ(usd/price) したもの（平均取得単価の分母）。
    名前への変換と人単位の合算は呼び出し側（chain/run.py）がやる。
    """
    return [dict(r) for r in conn.execute(
        "SELECT wallet,"
        " COALESCE(SUM(CASE WHEN side='buy' THEN usd END),0) AS buy_usd,"
        " COALESCE(SUM(CASE WHEN side='sell' THEN usd END),0) AS sell_usd,"
        " MIN(CASE WHEN side='buy' THEN ts END) AS first_buy_ts,"
        " MAX(ts) AS last_ts,"
        " COALESCE(SUM(CASE WHEN side='buy' AND price>0 THEN usd/price END),0) AS buy_qty,"
        " SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS n_buys,"
        " SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS n_sells"
        " FROM wallet_trades WHERE chain=? AND address=? AND ts>=?"
        " GROUP BY wallet ORDER BY buy_usd DESC",
        (chain, address, int(since)))]


def prune_wallet_trades(conn, now, days=14):
    conn.execute("DELETE FROM wallet_trades WHERE ts < ?", (int(now - days * 86400),))


def insert_mark(conn, chain, address, ts, memo, source="telegram"):
    """Tomato の目視メモ（/mark）。Bot の通知と突き合わせるための正解ラベル。"""
    conn.execute("INSERT INTO marks(ts,chain,address,memo,source) VALUES(?,?,?,?,?)",
                 (int(ts), chain, address, memo or "", source))


def recent_marks(conn, limit=20):
    return [dict(r) for r in conn.execute(
        "SELECT ts,chain,address,memo FROM marks ORDER BY ts DESC LIMIT ?", (int(limit),))]


def today_start(now=None):
    """ローカル時刻の当日 0:00（unix 秒）。"""
    t = time.localtime(now if now is not None else time.time())
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
