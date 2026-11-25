"""Discord webhook 알림. 실패해도 예외를 던지지 않고 False를 돌려준다."""
import json
import time
import urllib.error
import urllib.request

from .state import load_env

LIMIT = 1900  # Discord 메시지 한도 2000자에 여유를 둔다


def _chunks(text):
    while text:
        cut = text.rfind("\n", 0, LIMIT) if len(text) > LIMIT else len(text)
        cut = cut if cut > 0 else LIMIT
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def _post(url, content):
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "ctfm-webhook/1.0"}
    )
    for _ in range(3):
        try:
            urllib.request.urlopen(request, timeout=15).close()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 1)), 10))
    raise OSError("Discord rate limit")


def discord(store, content):
    url = load_env(store.root).get("DISCORD_WEBHOOK_URL")
    if not url:
        store.emit("NOTIFY_ERROR", "DISCORD_WEBHOOK_URL이 .env에 없음", echo=False)
        return False
    try:
        for chunk in _chunks(content):
            _post(url, chunk)
        return True
    except (OSError, ValueError) as e:
        store.emit("NOTIFY_ERROR", f"Discord 전송 실패: {e}", echo=False)
        return False
