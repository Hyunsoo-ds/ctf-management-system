"""Discord 포럼 채널 webhook 알림. 문제마다 포스트(스레드) 하나에 모은다. 실패해도 예외를 던지지 않고 False를 돌려준다.
글은 webhook으로 쓰고, 봇 토큰은 포럼의 태그 목록을 읽는 데만 쓴다."""
import fcntl
import json
import time
import urllib.error
import urllib.request

from .state import load_env

LIMIT = 1900  # Discord 메시지 한도 2000자에 여유를 둔다
API = "https://discord.com/api/v10"  # 테스트는 .env의 DISCORD_API로 바꾼다


def _chunks(text):
    while text:
        cut = text.rfind("\n", 0, LIMIT) if len(text) > LIMIT else len(text)
        cut = cut if cut > 0 else LIMIT
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def _request(url, payload=None, token=None):
    """payload가 있으면 POST, 없으면 GET. token은 봇 토큰이다."""
    body = None if payload is None else json.dumps({**payload, "allowed_mentions": {"parse": []}}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (ctfm, 1.0)"}
    if token:
        headers["Authorization"] = f"Bot {token}"
    request = urllib.request.Request(url, data=body, headers=headers)
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = json.loads(e.read() or b"{}")
            if e.code != 429:
                e.msg = f"{e.msg}: {detail.get('message', '')}"  # 예: "Unknown Channel", "401: Unauthorized"
                raise
            time.sleep(min(float(detail.get("retry_after", 1)), 10))
    raise OSError("Discord rate limit")


def new_post(store, env, url, title, category):
    """새 포스트 요청에 넣을 값. 포럼의 태그 중 이름이 분야와 같은 것(없으면 misc)을 붙인다.
    webhook은 태그 ID만 받고 태그 목록은 주지 않아서, 목록은 봇 토큰(.env의 DISCORD_BOT_TOKEN)으로 채널 정보를 읽어 얻는다.
    토큰이 없거나 조회에 실패하면 태그 없이 만든다(알림은 잃지 않는다)."""
    post = {"thread_name": title[:100]}
    token = env.get("DISCORD_BOT_TOKEN")
    if token:
        try:
            channel = _request(url)["channel_id"]
            found = _request(f"{env.get('DISCORD_API', API)}/channels/{channel}", token=token)["available_tags"]
            tags = {t["name"].lower(): t["id"] for t in found}
            tag = tags.get(category) or tags.get("misc")
            if tag:
                post["applied_tags"] = [tag]
        except (OSError, ValueError, KeyError) as e:
            store.emit("NOTIFY_ERROR", "포럼 태그 조회 실패, 태그 없이 포스트를 만듦 "
                                       f"(.env의 DISCORD_BOT_TOKEN과 봇의 채널 보기 권한 확인): {e}", echo=False)
    return post


def discord(store, content, ch=None):
    """문제(ch)의 포스트에 보낸다. 없으면 `[분야] 문제이름`으로 만들고 분야 태그를 붙인다.
    ch가 없으면 대회 이름의 공통 포스트(misc 태그)에 보낸다.
    포럼 채널 webhook은 thread_name(새 포스트)이나 thread_id(기존 포스트) 없이는 받지 않는다.
    호출자는 store.locked() 밖에 있어야 한다."""
    env = load_env(store.root)
    url = env.get("DISCORD_WEBHOOK_URL")
    if not url:
        store.emit("NOTIFY_ERROR", "DISCORD_WEBHOOK_URL이 .env에 없음", echo=False)
        return False
    key, title = (str(ch["id"]), f"[{ch['category']}] {ch['name']}") if ch else ("", store.root.name)
    try:
        with open(store.meta / "notify.lock", "w") as lock:  # 같은 문제의 첫 알림 둘이 포스트를 두 개 만들지 않게
            fcntl.flock(lock, fcntl.LOCK_EX)
            thread = store.state().get("threads", {}).get(key)
            for chunk in _chunks(content):
                if thread:
                    try:
                        _request(f"{url}?thread_id={thread}", {"content": chunk})
                        continue
                    except urllib.error.HTTPError as e:
                        if e.code not in (400, 404):  # 사용자가 포스트를 지웠으면 Unknown Channel: 새로 만든다
                            raise
                post = new_post(store, env, url, title, ch["category"] if ch else "misc")
                thread = _request(f"{url}?wait=true", {"content": chunk, **post})["channel_id"]
                with store.locked() as (cfg, st):
                    st.setdefault("threads", {})[key] = thread
        return True
    except (OSError, ValueError, KeyError) as e:
        store.emit("NOTIFY_ERROR", f"Discord 전송 실패 (포럼 채널 webhook이어야 함): {e}", echo=False)
        return False
