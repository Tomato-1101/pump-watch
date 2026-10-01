"""高速レーンの永続化。既存の state.db に chain_* の 3 表を足すだけ。

既存の表（tokens / snapshots / events / kv）はそのまま共有する。ここで足すのは
「毎回 eth_call で引き直したくないメタ」と「再起動しても 24h 窓を失わない 1 分バケット」。
"""
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_pairs (
  chain TEXT NOT NULL,
  pair TEXT NOT NULL,
  token0 TEXT,
  token1 TEXT,
  dec0 INTEGER,
  dec1 INTEGER,
  dex TEXT,
  quote TEXT,
  base TEXT,
  PRIMARY KEY (chain, pair)
);
CREATE TABLE IF NOT EXISTS chain_tokens (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  symbol TEXT,
  decimals INTEGER,
  total_supply REAL,
  created_ts INTEGER,
  launchpad TEXT,
  quote TEXT,
  PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS chain_buckets (
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  minute INTEGER NOT NULL,
  buys INTEGER,
  sells INTEGER,
  buyers INTEGER,
  vol_usd REAL,
  last_price REAL,
  high REAL,
  PRIMARY KEY (chain, address, minute)
);
CREATE INDEX IF NOT EXISTS idx_chain_buckets_minute ON chain_buckets (minute);
"""


def ensure(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# --- ペア / トークンのメタ --------------------------------------------------

def load_pairs(conn, chain):
    """{pair: メタ or None}。quote が NULL の行は「対象外と分かっているペア」。"""
    out = {}
    for r in conn.execute(
            "SELECT pair,token0,token1,dec0,dec1,dex,quote,base FROM chain_pairs WHERE chain=?",
            (chain,)):
        out[r["pair"]] = None if not r["quote"] else {
            "t0": r["token0"], "t1": r["token1"], "d0": r["dec0"], "d1": r["dec1"],
            "dex": r["dex"], "quote": r["quote"], "base": r["base"]}
    return out


def save_pair(conn, chain, pair, meta):
    conn.execute(
        "INSERT OR REPLACE INTO chain_pairs(chain,pair,token0,token1,dec0,dec1,dex,quote,base)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (chain, pair, (meta or {}).get("t0"), (meta or {}).get("t1"),
         (meta or {}).get("d0"), (meta or {}).get("d1"), (meta or {}).get("dex"),
         (meta or {}).get("quote"), (meta or {}).get("base")))


def load_tokens(conn, chain):
    out = {}
    for r in conn.execute(
            "SELECT address,symbol,decimals,total_supply,created_ts,launchpad,quote"
            " FROM chain_tokens WHERE chain=?", (chain,)):
        out[r["address"]] = {"symbol": r["symbol"], "decimals": r["decimals"],
                             "supply": r["total_supply"], "created_ts": r["created_ts"],
                             "launchpad": r["launchpad"], "quote": r["quote"]}
    return out


def save_token(conn, chain, address, meta):
    conn.execute(
        "INSERT OR REPLACE INTO chain_tokens(chain,address,symbol,decimals,total_supply,"
        "created_ts,launchpad,quote) VALUES(?,?,?,?,?,?,?,?)",
        (chain, address, meta.get("symbol"), meta.get("decimals"), meta.get("supply"),
         int(meta["created_ts"]) if meta.get("created_ts") else None,
         meta.get("launchpad"), meta.get("quote")))


# --- 1 分バケット ----------------------------------------------------------

def save_buckets(conn, chain, rows):
    """rows: [(address, minute, buys, sells, buyers, vol, last_price, high), ...]"""
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO chain_buckets(chain,address,minute,buys,sells,buyers,"
        "vol_usd,last_price,high) VALUES(?,?,?,?,?,?,?,?,?)",
        [(chain, *r) for r in rows])


def load_buckets(conn, chain, since_minute):
    """{address: {minute: [buys, sells, buyers, vol, last_price, high]}}"""
    out = {}
    for r in conn.execute(
            "SELECT address,minute,buys,sells,buyers,vol_usd,last_price,high"
            " FROM chain_buckets WHERE chain=? AND minute>=?", (chain, int(since_minute))):
        out.setdefault(r["address"], {})[r["minute"]] = [
            r["buys"] or 0, r["sells"] or 0, r["buyers"] or 0, r["vol_usd"] or 0.0,
            r["last_price"], r["high"]]
    return out


def prune_buckets(conn, now=None, days=2):
    before = int(((now or time.time()) - days * 86400) // 60)
    conn.execute("DELETE FROM chain_buckets WHERE minute < ?", (before,))
