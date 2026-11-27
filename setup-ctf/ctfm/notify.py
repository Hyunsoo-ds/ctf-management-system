"""Discord 포럼 채널 webhook 알림. 문제마다 포스트(스레드) 하나에 모은다. 실패해도 예외를 던지지 않고 False를 돌려준다."""
import fcntl
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


def _post(url, payload):
    body = json.dumps({**payload, "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "ctfm-webhook/1.0"}
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = json.loads(e.read() or b"{}")
            if e.code != 429:
                e.msg = f"{e.msg}: {detail.get('message', '')}"  # 예: "A tag is required to create a forum post in this channel"
                raise
            time.sleep(min(float(detail.get("retry_after", 1)), 10))
    raise OSError("Discord rate limit")


def forum_tags(env):
    """.env의 `DISCORD_FORUM_TAGS=pwn:<태그 ID>,web:<태그 ID>,...`를 {분야: 태그 ID}로 읽는다.
    webhook으로는 태그 이름을 쓸 수 없고 ID만 받으며, ID는 webhook으로 조회할 수 없어 설정으로 받는다."""
    pairs = (item.split(":", 1) for item in env.get("DISCORD_FORUM_TAGS", "").split(",") if ":" in item)
    return {name.strip().lower(): tag.strip() for name, tag in pairs}


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
    tag = forum_tags(env).get(ch["category"] if ch else "misc")
    new_post = {"thread_name": title[:100], **({"applied_tags": [tag]} if tag else {})}
    try:
        with open(store.meta / "notify.lock", "w") as lock:  # 같은 문제의 첫 알림 둘이 포스트를 두 개 만들지 않게
            fcntl.flock(lock, fcntl.LOCK_EX)
            thread = store.state().get("threads", {}).get(key)
            for chunk in _chunks(content):
                if thread:
                    try:
                        _post(f"{url}?thread_id={thread}", {"content": chunk})
                        continue
                    except urllib.error.HTTPError as e:
                        if e.code not in (400, 404):  # 사용자가 포스트를 지웠으면 Unknown Channel: 새로 만든다
                            raise
                thread = _post(f"{url}?wait=true", {"content": chunk, **new_post})["channel_id"]
                with store.locked() as (cfg, st):
                    st.setdefault("threads", {})[key] = thread
        return True
    except (OSError, ValueError, KeyError) as e:
        store.emit("NOTIFY_ERROR", f"Discord 전송 실패 (포럼 채널 webhook이어야 하고, 태그가 필수인 포럼이면 "
                                   f".env의 DISCORD_FORUM_TAGS가 필요함): {e}", echo=False)
        return False
