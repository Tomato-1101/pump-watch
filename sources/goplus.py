"""GoPlus Token Security（無料・鍵不要）。通知に載せる「安全確認」の供給元。

`GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses=<小文字>`
実測（2026-09-18）: BSC は 0.1 秒・連続 6 回 OK・`code:1` が成功。
robinhood(4663) は `code:1` だが `result:{}`、arc(5042) は `code:2022`（未対応チェーン）。
通知経路から呼ぶので**再試行しない**（遅れるくらいなら「未取得」と出す）。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.gopluslabs.io/api/v1/token_security"

# GoPlus のチェーン ID。ここに無いチェーンはそもそも呼ばない。
CHAIN_IDS = {"eth": "1", "bsc": "56", "base": "8453", "robinhood": "4663", "arc": "5042"}


def fetch(chain, address, timeout=2):
    """(結果 dict, 状態) を返す。状態は "ok" / "unsupported" / "error"。

    "unsupported" は「このチェーンは GoPlus が持っていない」＝以後呼ばないための印。
    銘柄が見つからないだけの場合も result が空になるが、チェーン単位の判定は
    呼び出し側が起動後の初回だけ行う（enrich.security）。
    """
    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return None, "unsupported"
    addr = str(address or "").lower()
    url = f"{API}/{chain_id}?" + urllib.parse.urlencode({"contract_addresses": addr})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "pump-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return None, "error"
    if payload.get("code") != 1:
        return None, "unsupported"
    result = payload.get("result") or {}
    if not isinstance(result, dict) or not result:
        return None, "unsupported"
    for k, v in result.items():
        if str(k).lower() == addr and isinstance(v, dict):
            return v, "ok"
    return None, "error"
