"""four.meme の公開 API（鍵不要）。通知に付ける画像だけを取る。

実測 2026-09-18:
- `GET /meme-api/v1/private/token/get?address=<addr>` が匿名で 200。BSC のアドレスなら
  launchpad が four.meme でなくても `data.image` が入っていることがある。
- 知らないアドレスでも 200 で `{"code":0,"msg":"success"}` だけが返る（`data` が無い）。
  これは「画像なし」として扱い、呼び出し側でキャッシュして二度撃たない。
- 画像は `https://static.four.meme/market/<id>.(png|jpg|webp)`。UA なしの匿名 GET でも
  200（image/png）で返るので、Telegram の sendPhoto が取りに行ける。

レート制限は公開されていないので、連続呼び出しは MIN_GAP 秒あける。
"""
import time

from . import http

BASE = "https://four.meme/meme-api/v1/private/token/get"
MIN_GAP = 1.0       # 連続呼び出しの最小間隔（秒）
IMAGE_HOST = "https://static.four.meme/"

_last_call = 0.0


def image(address, timeout=2):
    """画像 URL。無ければ None。通知を待たせないので再試行はしない。"""
    global _last_call
    gap = MIN_GAP - (time.time() - _last_call)
    if gap > 0:
        time.sleep(min(gap, MIN_GAP))
    _last_call = time.time()
    data, _status = http.get_json(f"{BASE}?address={address}", timeout=timeout, attempts=1)
    url = ((data or {}).get("data") or {}).get("image")
    return url if url and url.startswith(IMAGE_HOST) else None
