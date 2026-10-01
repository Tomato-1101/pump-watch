"""公開 BSC RPC への JSON-RPC（urllib だけ・鍵不要）。

実測（2026-09-18）で分かっている癖:
  - User-Agent が urllib の既定（Python-urllib）だと publicnode / drpc は 403 を返す。必ず付ける。
  - publicnode / blockrazor / blxrbdn は 1 ブロック範囲なら topics だけの eth_getLogs を
    通す（22KB / 30〜40ms・件数は 3 者一致）。dataseed は同じ要求を "limit exceeded" で
    断るので、こちらは getBlockReceipts に落とす。
  - publicnode は複数バックエンドの前段で、たまに head に追いついていない 1 台に当たり
    "block range extends beyond current head block" を返す。ノード自体は生きているので
    ブロック扱い（block_seconds のスキップ）にはせず、その場で次の topics 対応ノードへ
    回すだけにする。次のポーリングではまた publicnode から試す。
  - drpc は 2 回/秒で 429 が多発するので最終フォールバックにしか置かない。
  - 不採用: meowrpc は getLogs 非対応、ankr は鍵必須、blastapi / zan は即 429、
    1rpc は 0.5〜1.6 秒、tatum は 0.55 秒。
  - JSON-RPC のバッチ（配列で投げる）は publicnode で通る。メタ解決の eth_call をまとめる。

失敗はサイクルを止めない。呼び出し側が (結果, エラー文字列) を見て次へ進む。
"""
import json
import time
import urllib.error
import urllib.request

# ブラウザを装わず、誰のクライアントか分かる UA を名乗る（urllib 既定の UA は 403 で弾かれる）
UA = "pump-watch/1.0 (+https://github.com/Tomato-1101/pump-watch)"

# name, url, topics だけの getLogs が通るか
PROVIDERS = (
    ("publicnode", "https://bsc-rpc.publicnode.com", True),
    ("blockrazor", "https://bsc.blockrazor.xyz", True),
    ("blxrbdn", "https://bsc.rpc.blxrbdn.com", True),
    ("dataseed", "https://bsc-dataseed.bnbchain.org", False),
    ("drpc", "https://bsc.drpc.org", False),
)


class Provider:
    def __init__(self, name, url, topic_logs):
        self.name = name
        self.url = url
        self.topic_logs = topic_logs
        self.ok = 0
        self.fail = 0
        self.ms_total = 0.0
        self.blocked_until = 0.0

    @property
    def avg_ms(self):
        return self.ms_total / self.ok if self.ok else 0.0

    @property
    def rate(self):
        n = self.ok + self.fail
        return 100.0 * self.ok / n if n else 0.0


class Rpc:
    """プロバイダーを順に試す JSON-RPC クライアント。

    失敗したプロバイダーは block_seconds だけ飛ばす（落ちているノードに毎ブロック
    ぶつかると 1 ブロックぶんの猶予 0.45 秒を食い潰すため）。
    """

    def __init__(self, providers=PROVIDERS, timeout=5, block_seconds=20):
        self.providers = [Provider(*p) for p in providers]
        self.timeout = timeout
        self.block_seconds = block_seconds

    # --- 下回り -----------------------------------------------------------

    def _post(self, provider, payload, timeout):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            provider.url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": UA,
                     "Accept": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            provider.ok += 1
            provider.ms_total += (time.time() - t0) * 1000
            return data, None
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            err = f"HTTP {e.code}"
        except Exception as e:  # 接続断・タイムアウト・JSON 崩れ
            err = f"{type(e).__name__}"
        provider.fail += 1
        provider.blocked_until = time.time() + self.block_seconds
        return None, err

    def _usable(self, now, topic_logs):
        for p in self.providers:
            if topic_logs and not p.topic_logs:
                continue
            if p.blocked_until > now:
                continue
            yield p

    # --- 呼び出し ---------------------------------------------------------

    def call(self, method, params, topic_logs=False, timeout=None):
        """(結果, エラー) を返す。全プロバイダーが駄目なら (None, エラー)。

        topic_logs=True は「address を指定しない getLogs」。対応しているノードだけを使う。
        """
        now = time.time()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = "no provider"
        for p in self._usable(now, topic_logs):
            data, err = self._post(p, payload, timeout or self.timeout)
            if err:
                last = f"{p.name}: {err}"
                continue
            if isinstance(data, dict) and data.get("error"):
                # ノードは生きているがこの要求は通らない（範囲超過等）。次のノードへ。
                last = f"{p.name}: {data['error'].get('message')}"
                continue
            return (data or {}).get("result"), None
        return None, last

    def batch(self, calls, timeout=None):
        """[(method, params), ...] を 1 リクエストで投げて結果リストを返す。

        バッチ非対応・一部失敗のときは None を並べる（呼び出し側が個別に諦める）。
        """
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                   for i, (m, p) in enumerate(calls)]
        now = time.time()
        for prov in self._usable(now, False):
            data, err = self._post(prov, payload, timeout or self.timeout)
            if err or not isinstance(data, list):
                continue
            out = [None] * len(calls)
            for item in data:
                i = item.get("id")
                if isinstance(i, int) and 0 <= i < len(out) and not item.get("error"):
                    out[i] = item.get("result")
            return out
        return [None] * len(calls)

    # --- 便利メソッド -----------------------------------------------------

    def block_number(self):
        r, err = self.call("eth_blockNumber", [], timeout=4)
        return (int(r, 16) if isinstance(r, str) else None), err

    def logs_by_topics(self, block, topics):
        """1 ブロックぶんのログを topic0 の集合で引く（address 指定なし）。"""
        h = hex(block)
        return self.call("eth_getLogs", [{"fromBlock": h, "toBlock": h,
                                          "topics": [list(topics)]}],
                         topic_logs=True, timeout=5)

    def block_receipts(self, block):
        """publicnode が落ちたときの代替。ブロックの全レシートからログを取り出す。"""
        return self.call("eth_getBlockReceipts", [hex(block)], timeout=8)

    def stats_line(self):
        parts = []
        for p in self.providers:
            if p.ok or p.fail:
                parts.append(f"{p.name}:{p.avg_ms:.0f}ms/{p.rate:.0f}%")
        return " ".join(parts) or "-"
