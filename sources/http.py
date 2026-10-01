"""urllib だけの JSON GET。429 / 5xx は 1 回だけ短く待って再試行する。

呼び出し側はサイクルを止めたくないので、失敗は例外ではなく (None, status) で返す。
"""
import json
import time
import urllib.error
import urllib.request

RETRY_STATUS = (429, 500, 502, 503, 504)


def get_json(url, headers=None, timeout=12, retry_wait=1.5, ua="pump-watch/1.0", attempts=2):
    """(データ, HTTP ステータス) を返す。失敗時のデータは None。

    ステータスは呼び出し側がレート制限の判断に使う（429 を受けたら当面撃たない等）。
    attempts=1 は再試行なし（通知経路のように待てないところで使う）。
    """
    hdr = {"Accept": "application/json", "User-Agent": ua}
    hdr.update(headers or {})
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode()), r.status
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                e.read()
            except Exception:
                pass
        except Exception:
            status = 0  # 接続断・タイムアウト・JSON 崩れ
        if attempt + 1 < attempts and (status in RETRY_STATUS or status == 0):
            time.sleep(retry_wait)
            continue
        return None, status
    return None, status


def chunked(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]
