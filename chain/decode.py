"""BSC のログを「1 件の取引」に変換する。

対応する源（2026-09-18 に実ログで確定。topic0 と引数の並びは README に記録）:
  PancakeSwap V2 Swap / Sync   価格・売買・USD 額・流動性（reserves）
  PancakeSwap V3 Swap          価格（sqrtPriceX96）・売買・USD 額
  four.meme TokenManager2      ボンディングカーブ上の Purchase / Sale / Create
  flap 0xe2ce6ab8…             銘柄の属性だけ（TokenCreated / TokenBought の印）

flap は取引として数えない。実 tx（0x1c8f498d…）で確認したとおり flap のカーブは
**本物の PancakeSwap V2 ペア（quote は WBNB）**で、同じ tx に V2 Swap と TokenBought の
両方が出る。両方を数えると出来高も件数も 2 倍になるので、取引は V2 側だけで数え、
flap のイベントは「どのランチパッド発か・いつ作られたか」の印にだけ使う。

USD 換算は quote トークン（WBNB・USDT・USDC・BUSD・BNCB）の建値でしかできないので、
どちらの側も quote でないペアは無視する。WBNB と BNCB のレートは自分が見た
安定コイン建てペアの Sync から更新する（起動時だけ DexScreener で初期化）。
"""
import threading
from dataclasses import dataclass

# --- topic0 ---------------------------------------------------------------

V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
FM_PURCHASE = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942"
FM_SALE = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"
FM_CREATE = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20"
FLAP_BOUGHT = "0xa800a2038683844fac66747f771bfdfae862eb28b16bcfa387afa9fbacce8ff7"
FLAP_SOLD = "0x03a4693e592f5e75dc7c136acb39b146d2b4966c0e509c34f362dee02b3b861a"
FLAP_CREATE = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"

TOPICS = (V2_SWAP, V3_SWAP, SYNC, FM_PURCHASE, FM_SALE, FM_CREATE,
          FLAP_BOUGHT, FLAP_SOLD, FLAP_CREATE)

FOURMEME = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
FLAP = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"

# --- トークン -------------------------------------------------------------

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
BNCB = "0x4902c5ebc598265ed2212b559b042de8a5eeec3f"  # flap 卒業後のペアの quote

STABLES = {USDT: "USDT", USDC: "USDC", BUSD: "BUSD"}
FLOATING = {WBNB: "WBNB", BNCB: "BNCB"}
QUOTES = dict(STABLES, **FLOATING)
# 安定コイン建ての大きいペア。ここの Sync だけを浮動 quote のレート源にする
RATE_PAIRS = {"0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae": WBNB,   # WBNB/USDT (V2)
              "0xb754f10cebf50b8862444cf4d6c1cc31a3b71c18": BNCB}  # BNCB/USDT (V2)

# eth_call のセレクタ（keccak256 で確認済み）
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"
SEL_SUPPLY = "0x18160ddd"
SEL_SYMBOL = "0x95d89b41"
SEL_TOKENINFOS = "0xe684626b"  # four.meme TokenManager2 _tokenInfos(address)

Q96 = 1 << 96


# --- ABI の素デコード ------------------------------------------------------

def words(data):
    h = (data or "0x")[2:]
    return [h[i:i + 64] for i in range(0, len(h) - len(h) % 64, 64)]


def u(w):
    return int(w, 16) if w else 0


def i256(w):
    v = int(w, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def addr(w):
    return "0x" + w[-40:].lower()


def abi_string(ws, byte_offset):
    """動的 string を取り出す（オフセットは data 先頭からのバイト数）。"""
    k = byte_offset // 32
    if k >= len(ws):
        return ""
    n = u(ws[k])
    raw = "".join(ws[k + 1:k + 1 + (n + 31) // 32])[:n * 2]
    try:
        return bytes.fromhex(raw).decode("utf-8", "replace")
    except ValueError:
        return ""


def _to(log):
    """Swap の受取先（topics[2]）。ルーター経由だと本人でないこともある近似値。"""
    t = log.get("topics") or []
    return addr(t[2]) if len(t) > 2 else (log.get("transactionHash") or "")


# --- 各イベント -----------------------------------------------------------

def v2_swap(log):
    w = words(log.get("data"))
    return (u(w[0]), u(w[1]), u(w[2]), u(w[3])) if len(w) >= 4 else None


def v3_swap(log):
    w = words(log.get("data"))
    return (i256(w[0]), i256(w[1]), u(w[2])) if len(w) >= 3 else None


def sync(log):
    w = words(log.get("data"))
    return (u(w[0]), u(w[1])) if len(w) >= 2 else None


def fourmeme_trade(log):
    """TokenPurchase / TokenSale。price は quote/token（どちらも 1e18 倍）。

    実 tx で検算済み: cost(word4) == amount(word3) * price(word2) / 1e18。
    """
    w = words(log.get("data"))
    if len(w) < 6:
        return None
    return {"token": addr(w[0]), "account": addr(w[1]), "price": u(w[2]) / 1e18,
            "amount": u(w[3]) / 1e18, "cost": u(w[4]) / 1e18,
            "side": "buy" if log["topics"][0] == FM_PURCHASE else "sell"}


def fourmeme_create(log):
    w = words(log.get("data"))
    if len(w) < 6:
        return None
    return {"creator": addr(w[0]), "token": addr(w[1]), "supply": u(w[5]) / 1e18,
            "name": abi_string(w, u(w[3])), "symbol": abi_string(w, u(w[4]))}


def flap_trade(log):
    """TokenBought / TokenSold(uint256 ts, address token, address who, uint256 amount,
    uint256 bnb, uint256 fee, uint256 …)。取引としては数えず、銘柄の印にだけ使う。"""
    w = words(log.get("data"))
    if len(w) < 5:
        return None
    amount = u(w[3]) / 1e18
    cost = u(w[4]) / 1e18
    return {"token": addr(w[1]), "account": addr(w[2]), "amount": amount, "cost": cost,
            "price": (cost / amount) if amount else 0.0,
            "side": "buy" if log["topics"][0] == FLAP_BOUGHT else "sell"}


def flap_create(log):
    """TokenCreated(uint256 ts, address creator, uint256 id, address token,
    string name, string symbol, string uri)。"""
    w = words(log.get("data"))
    if len(w) < 7:
        return None
    return {"created_ts": u(w[0]), "creator": addr(w[1]), "token": addr(w[3]),
            "name": abi_string(w, u(w[4])), "symbol": abi_string(w, u(w[5]))}


def pick_quote(t0, t1):
    """ペアの (quote, base)。どちらも quote でなければ None（USD 換算できないので対象外）。

    どちらも quote のとき（WBNB/USDT 等）は安定コインの側を quote にする。
    """
    q0, q1 = t0 in QUOTES, t1 in QUOTES
    if not (q0 or q1):
        return None
    if q0 and q1:
        quote = t0 if (t0 in STABLES and t1 not in STABLES) else t1
    else:
        quote = t0 if q0 else t1
    return quote, (t1 if quote == t0 else t0)


def price_from_sqrt(sqrt_x96, d0, d1):
    """token1 / token0 の価格（人間単位）。V3 はスワップ後のプール価格を持っている。"""
    if not sqrt_x96:
        return None
    return (sqrt_x96 / Q96) ** 2 * (10 ** (d0 - d1))


# --- 取引 -----------------------------------------------------------------

@dataclass
class Trade:
    ts: float
    address: str          # 銘柄（base トークン）
    side: str             # "buy" / "sell"
    usd: float
    trader: str
    price: float          # USD
    block: int
    dex: str
    liq: float = None     # USD（V2 の reserves から。取れないときは None）
    supply: float = None  # 総供給（人間単位。four.meme / flap の Create で分かる）
    symbol: str = None
    launchpad: str = None
    created_ts: float = None


class Rates:
    """quote トークンの USD レート。安定コインは 1.0 固定。"""

    def __init__(self):
        self._r = {a: 1.0 for a in STABLES}
        self._ts = {}

    def set(self, token, usd, ts=0.0):
        if usd and usd > 0:
            self._r[token] = usd
            self._ts[token] = ts

    def get(self, token):
        return self._r.get(token)

    def age(self, token, now):
        t = self._ts.get(token)
        return None if t is None else now - t

    def snapshot(self):
        return dict(self._r)


class Decoder:
    """ログ列 → Trade 列。ペア/トークンのメタが要るぶんは解決待ちに積む。

    poll スレッド（decode_logs）と resolver スレッド（apply_meta）から触るのでロックで守る。
    """

    def __init__(self, rates=None, buffer_limit=120):
        self.rates = rates or Rates()
        self.pairs = {}    # pair -> {"t0","t1","d0","d1","dex","quote","base"} / None(対象外)
        self.tokens = {}   # token -> {"decimals","supply","symbol","launchpad","quote"}
        self.hints = {}    # Create イベントで先に分かったぶん（解決時にこれを混ぜる）
        self.pair_dex = {}  # 未解決ペアがどっちの DEX だったか（Swap の topic0 から）
        self.buffered = {}  # 未解決キー -> [(log, ts, block), ...]
        self.need = []     # 解決待ちキー（("pair", addr) / ("token", addr) / ("fm", addr)）
        self.lock = threading.Lock()
        self.buffer_limit = buffer_limit

    # --- 解決待ち ---------------------------------------------------------

    def _want(self, key, log, ts, block):
        buf = self.buffered.setdefault(key, [])
        if len(buf) < self.buffer_limit:
            buf.append((log, ts, block))
        if key not in self.need:
            self.need.append(key)

    def take_need(self, limit=20):
        with self.lock:
            out, self.need = self.need[:limit], self.need[limit:]
        return out

    def pending_count(self):
        with self.lock:
            return len(self.need)

    def hint(self, token):
        with self.lock:
            return dict(self.hints.get(token) or {})

    def apply_meta(self, kind, address, meta):
        """解決結果を入れて、溜めてあったログを decode し直す（Trade のリストを返す）。"""
        with self.lock:
            if kind == "pair":
                self.pairs[address] = meta
            else:
                self.tokens[address] = meta
            buf = self.buffered.pop((kind, address), [])
        out = []
        for log, ts, block in buf:
            t = self.decode(log, ts, block)
            if t:
                out.append(t)
        return out

    # --- 本体 -------------------------------------------------------------

    def decode_logs(self, logs, block_ts, block):
        out = []
        for log in logs:
            t = self.decode(log, block_ts, block)
            if t:
                out.append(t)
        return out

    def decode(self, log, ts, block):
        topic = (log.get("topics") or [""])[0]
        src = (log.get("address") or "").lower()
        if topic == SYNC:
            self._sync(src, log, ts)
            return None
        if topic in (V2_SWAP, V3_SWAP):
            return self._swap(src, log, ts, block, topic)
        if src == FOURMEME:
            return self._fourmeme(log, ts, block, topic)
        if src == FLAP:
            return self._flap(log, ts, block, topic)
        return None

    # --- PancakeSwap ------------------------------------------------------

    def _sync(self, pair, log, ts):
        """レート源ペアの reserves から WBNB / BNCB の USD 値を更新し、
        対象ペアの流動性（quote 側 × 2）を覚える。"""
        r = sync(log)
        if not r:
            return
        with self.lock:
            meta = self.pairs.get(pair)
        if not meta:
            return
        d0, d1 = meta["d0"], meta["d1"]
        h0, h1 = r[0] / 10 ** d0, r[1] / 10 ** d1
        floating = RATE_PAIRS.get(pair)
        if floating:
            base_h, quote_h = (h0, h1) if meta["base"] == meta["t0"] else (h1, h0)
            if base_h > 0:
                self.rates.set(floating, quote_h / base_h, ts)
            return
        rate = self.rates.get(meta["quote"])
        if rate:
            quote_h = h1 if meta["quote"] == meta["t1"] else h0
            meta["liq"] = quote_h * rate * 2

    def _swap(self, pair, log, ts, block, topic):
        with self.lock:
            meta = self.pairs.get(pair, "?")
        if meta == "?":
            with self.lock:
                self.pair_dex[pair] = "pancake-v2" if topic == V2_SWAP else "pancake-v3"
                self._want(("pair", pair), log, ts, block)
            return None
        if meta is None:            # quote が無いペア（対象外）
            return None
        if meta["base"] in QUOTES:
            # WBNB/USDT のような quote 同士のペア。レート源であって銘柄ではない
            return None
        rate = self.rates.get(meta["quote"])
        if not rate:
            return None
        q_is_1 = meta["quote"] == meta["t1"]
        db_, dq = ((meta["d0"], meta["d1"]) if q_is_1 else (meta["d1"], meta["d0"]))
        if topic == V2_SWAP:
            v = v2_swap(log)
            if not v:
                return None
            a0in, a1in, a0out, a1out = v
            qin, qout = (a1in, a1out) if q_is_1 else (a0in, a0out)
            bin_, bout = (a0in, a0out) if q_is_1 else (a1in, a1out)
            if qin and bout:
                side, q_amt, b_amt = "buy", qin, bout
            elif bin_ and qout:
                side, q_amt, b_amt = "sell", qout, bin_
            else:
                return None
            q_h, b_h = q_amt / 10 ** dq, b_amt / 10 ** db_
            if b_h <= 0:
                return None
            price = q_h / b_h * rate
        else:
            v = v3_swap(log)
            if not v:
                return None
            a0, a1, sqrt_x96 = v
            q_amt, b_amt = ((a1, a0) if q_is_1 else (a0, a1))
            if q_amt > 0 and b_amt < 0:
                side = "buy"
            elif q_amt < 0 and b_amt > 0:
                side = "sell"
            else:
                return None
            q_h = abs(q_amt) / 10 ** dq
            p10 = price_from_sqrt(sqrt_x96, meta["d0"], meta["d1"])
            if not p10:
                return None
            price = (p10 if q_is_1 else 1.0 / p10) * rate
        with self.lock:
            tok = self.tokens.get(meta["base"]) or {}
            hint = self.hints.get(meta["base"]) or {}
            if not tok and ("base", meta["base"]) not in self.need:
                # ペアだけ分かっていて銘柄のメタが無い（総供給＝FDV が出せない）。
                # ペアを解決したときに取り損ねた/古いキャッシュのぶんをここで拾う。
                self.need.append(("base", meta["base"]))
        return Trade(ts=ts, address=meta["base"], side=side, usd=q_h * rate,
                     trader=_to(log), price=price, block=block, dex=meta["dex"],
                     liq=meta.get("liq"), supply=tok.get("supply"),
                     symbol=tok.get("symbol") or hint.get("symbol"),
                     launchpad=tok.get("launchpad") or hint.get("launchpad"),
                     created_ts=tok.get("created_ts") or hint.get("created_ts"))

    # --- ランチパッド -----------------------------------------------------

    def _token_meta(self, token, log, ts, block, kind="token"):
        """メタが揃っていれば返す。未解決なら解決待ちに積んで None。

        four.meme は quote（USDT 建てか BNB 建てか）が要るので、_tokenInfos で
        quote が入るまでは「未解決」扱いにする（V2 側の解決で入る supply だけの行と
        区別するため）。
        """
        with self.lock:
            meta = self.tokens.get(token)
            if not meta or (kind == "fm" and not meta.get("quote")):
                self._want((kind, token), log, ts, block)
                return None
        return meta

    def _fourmeme(self, log, ts, block, topic):
        if topic == FM_CREATE:
            c = fourmeme_create(log)
            if not c:
                return None
            with self.lock:
                self.hints[c["token"]] = {
                    "decimals": 18, "supply": c["supply"], "symbol": c["symbol"],
                    "launchpad": "four.meme", "created_ts": ts}
            return None
        if topic not in (FM_PURCHASE, FM_SALE):
            return None
        e = fourmeme_trade(log)
        if not e:
            return None
        meta = self._token_meta(e["token"], log, ts, block, "fm")
        if not meta:
            return None
        rate = self.rates.get(meta.get("quote"))
        if not rate:
            return None
        return Trade(ts=ts, address=e["token"], side=e["side"], usd=e["cost"] * rate,
                     trader=e["account"], price=e["price"] * rate, block=block,
                     dex="four.meme", supply=meta.get("supply"), symbol=meta.get("symbol"),
                     launchpad="four.meme", created_ts=meta.get("created_ts"))

    def _flap(self, log, ts, block, topic):
        """flap は取引を作らない（V2 Swap と同じ tx に出るため二重計上になる）。

        「flap 発の銘柄だ」「いつ作られたか」だけを hints に控え、V2 側の Trade に載せる。
        """
        if topic == FLAP_CREATE:
            c = flap_create(log)
            if c:
                with self.lock:
                    self.hints[c["token"]] = {
                        "symbol": c["symbol"], "launchpad": "flap",
                        "created_ts": c["created_ts"] or ts}
        elif topic in (FLAP_BOUGHT, FLAP_SOLD):
            e = flap_trade(log)
            if e:
                with self.lock:
                    self.hints.setdefault(e["token"], {})["launchpad"] = "flap"
        return None
