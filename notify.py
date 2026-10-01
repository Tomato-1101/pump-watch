"""Telegram Bot API（送信 / コマンド受信 / ボタン）。標準ライブラリのみ。

トークンは環境変数（PUMPWATCH_TG_TOKEN / PUMPWATCH_TG_CHAT_ID）か、無ければ macOS の
Keychain（service=変数名 / account=shared）から実行時に読むだけで、
ログにもファイルにも書かない（エラー文に混ざった場合も _scrub で伏せる）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.telegram.org"
MAX_LEN = 4000
CAPTION_LEN = 1024   # sendPhoto の caption 上限（Telegram の仕様）
# 手元のファイルを multipart で送るときの Content-Type（拡張子から決める）。
# Telegram は multipart のファイル名と型で画像形式を判断する
PHOTO_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}


def keychain(service, account="shared"):
    p = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def credentials():
    # 環境変数を先に見る（Keychain の無い Linux や CI でも動かすため）。無ければ macOS の Keychain
    token = os.environ.get("PUMPWATCH_TG_TOKEN") or keychain("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("PUMPWATCH_TG_CHAT_ID") or keychain("TELEGRAM_CHAT_ID")
    return token, chat_id


def _scrub(text, token):
    text = str(text)
    return text.replace(token, "***") if token else text


def multipart(params, files):
    """(本文, Content-Type) を組む。標準ライブラリだけで multipart/form-data にする。

    files は {フィールド名: (ファイル名, 中身 bytes, MIME)}。画像を URL ではなく
    本体で渡すために要る（Telegram に URL を取りに行かせると遅く・落ちやすい）。
    """
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in params.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                 f"{value}\r\n").encode()
    for name, (filename, content, mime) in files.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def api(token, method, params, timeout=30, files=None):
    """(結果 dict, エラー文字列) を返す。例外は投げない。

    files を渡すとフォームを multipart で組む（画像本体の送信）。
    """
    if files:
        data, ctype = multipart(params, files)
        req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data,
                                     headers={"Content-Type": ctype})
    else:
        data = urllib.parse.urlencode(params, doseq=True).encode()
        req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
        if payload.get("ok"):
            return payload.get("result"), None
        return None, _scrub(payload.get("description", "unknown error"), token)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        if e.code == 429:
            return None, {"retry_after": (payload.get("parameters") or {}).get("retry_after", 5)}
        return None, _scrub(payload.get("description", f"HTTP {e.code}"), token)
    except Exception as e:  # ネットワーク断・タイムアウト
        return None, _scrub(f"{type(e).__name__}: {e}", token)


def split_message(text, limit=MAX_LEN):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line[:limit])
    if cur:
        chunks.append(cur)
    return chunks


def trim_caption(text, limit=CAPTION_LEN):
    """caption の上限に収める。HTML タグの途中で切らないよう行単位で落とす。"""
    if len(text) <= limit:
        return text
    kept = []
    used = 0
    for line in text.split("\n"):
        if used + len(line) + 1 > limit - 2:
            break
        kept.append(line)
        used += len(line) + 1
    return ("\n".join(kept) + "\n…") if kept else text[:limit]


# --- ボタン ------------------------------------------------------------------

def inline_markup(rows):
    """[[{"text":…, "url"/"callback_data":…}, …], …] を reply_markup の文字列にする。"""
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def remove_keyboard():
    """画面下の常設キーボードを消す（以前に出したものを端末から片付けるため）。"""
    return json.dumps({"remove_keyboard": True}, ensure_ascii=False)


# --- 送信 --------------------------------------------------------------------

def _with_retry(token, method, params, timeout=30, files=None):
    """429 は retry_after 待ちで 1 回だけ再送する。(結果, エラー) を返す。"""
    res, err = api(token, method, params, timeout=timeout, files=files)
    if isinstance(err, dict):  # 429
        wait = min(int(err.get("retry_after", 5)) + 1, 60)
        print(f"  [telegram 429] wait {wait}s", file=sys.stderr, flush=True)
        time.sleep(wait)
        res, err = api(token, method, params, timeout=timeout, files=files)
    return res, err


def _err_text(err):
    """429 の dict も 1 行の文字列にする（alerts.error にそのまま残せるように）。"""
    return None if err is None else str(err)


def sent_ok(result):
    """送信関数の戻りを真偽に均す（True/False・Telegram の result・実測 dict のどれでも）。

    send / send_photo は Telegram の result（message_id を含む dict）を返し、
    watch.send_alert は送信の実測（"ok" を持つ dict）を返す。どちらも同じ扱いにする。
    """
    if isinstance(result, dict):
        return bool(result["ok"]) if "ok" in result else bool(result)
    return bool(result)


def photo_file(photo):
    """手元に落としてある画像なら {"photo": (名前, 中身, MIME)}。URL なら None。"""
    if not photo or "://" in photo or not os.path.isfile(photo):
        return None
    ext = os.path.splitext(photo)[1].lower()
    try:
        with open(photo, "rb") as f:
            content = f.read()
    except OSError:
        return None
    return {"photo": (os.path.basename(photo), content,
                      PHOTO_MIME.get(ext, "image/png"))}


def send_ex(text, token, chat_id, buttons=None):
    """(最後のチャンクの result, エラー文字列) を返す。1 つでも落ちたら失敗扱い。"""
    if not token or not chat_id:
        return None, "credentials missing"
    result, error = None, None
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        if buttons and i == len(chunks) - 1:
            params["reply_markup"] = buttons
        res, err = _with_retry(token, "sendMessage", params)
        if err:
            print(f"  [telegram error] {err}", file=sys.stderr, flush=True)
            error = _err_text(err)
            continue
        result = res if isinstance(res, dict) else result
    if error:
        return None, error
    # 送れたことは呼び出し側が真偽で見るので、result が空でも dict を返す
    return result or {"message_id": None}, None


def send(text, token, chat_id, buttons=None):
    """送信できたら Telegram の result（message_id / date）、送れなければ None。

    長い本文は 4000 字で分割する（ボタンは最後の 1 通に付く）。戻りは最後のチャンクの result。
    """
    return send_ex(text, token, chat_id, buttons)[0]


def send_photo_ex(photo, caption, token, chat_id, buttons=None, timeout=6):
    """(result, エラー文字列) を返す。photo が手元のファイルなら multipart で本体を送る。

    URL を渡すと Telegram のサーバーがそれを取りに行く方式になり、時間がかかったり
    取れずに失敗したりする。既定 6 秒で打ち切る（10 秒以内の送信を優先。失敗したら
    呼び出し側がテキストで送る＝既存の fallback）。
    """
    if not token or not chat_id or not photo:
        return None, "no photo"
    files = photo_file(photo)
    params = {"chat_id": chat_id, "caption": trim_caption(caption), "parse_mode": "HTML"}
    if not files:
        params["photo"] = photo
    if buttons:
        params["reply_markup"] = buttons
    res, err = _with_retry(token, "sendPhoto", params, timeout=timeout, files=files)
    if err:
        print(f"  [telegram sendPhoto] {err}", file=sys.stderr, flush=True)
        return None, _err_text(err)
    return (res if isinstance(res, dict) else {"message_id": None}), None


def send_photo(photo, caption, token, chat_id, buttons=None, timeout=6):
    """画像 1 枚 + caption。送れたら Telegram の result、失敗なら None。"""
    return send_photo_ex(photo, caption, token, chat_id, buttons, timeout)[0]


def edit_text(token, chat_id, message_id, text, buttons=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = buttons
    _res, err = api(token, "editMessageText", params)
    return err is None


def edit_markup(token, chat_id, message_id, buttons):
    params = {"chat_id": chat_id, "message_id": message_id}
    if buttons:
        params["reply_markup"] = buttons
    _res, err = api(token, "editMessageReplyMarkup", params)
    return err is None


def answer_callback(token, callback_id, text=""):
    """ボタンの「押した感」を返す。これを返さないと端末側がぐるぐる回り続ける。"""
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text[:200]
    _res, err = api(token, "answerCallbackQuery", params, timeout=10)
    return err is None


def set_my_commands(token, commands):
    """[(コマンド名, 説明), …] を Telegram のコマンド一覧（/ を打つと出る）に登録する。"""
    payload = [{"command": c, "description": d} for c, d in commands]
    _res, err = api(token, "setMyCommands",
                    {"commands": json.dumps(payload, ensure_ascii=False)}, timeout=15)
    if err:
        print(f"  [telegram setMyCommands] {err}", file=sys.stderr, flush=True)
    return err is None


def get_updates(token, offset=0, timeout=0, allowed_updates=("message",)):
    """(更新のリスト, エラー) を返す。timeout>0 ならロングポーリング。"""
    params = {"timeout": timeout,
              "allowed_updates": json.dumps(list(allowed_updates))}
    if offset:
        params["offset"] = offset
    res, err = api(token, "getUpdates", params, timeout=timeout + 15)
    if err:
        print(f"  [telegram getUpdates] {err}", file=sys.stderr, flush=True)
        return [], err
    return res or [], None
