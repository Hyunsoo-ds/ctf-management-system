"""Discord 포럼 채널 webhook 알림. 문제마다 포스트(스레드) 하나에 모은다. 실패해도 예외를 던지지 않고 False를 돌려준다.
글은 webhook으로 쓰고, 봇 토큰(.env의 DISCORD_BOT_TOKEN, 선택)은 포스트에 태그를 붙이고 풀린 문제의 포스트를 닫는 데 쓴다."""
import fcntl
import json
import time
import urllib.error
import urllib.request

from .state import load_env

LIMIT = 1900  # Discord 메시지 한도 2000자에 여유를 둔다
API = "https://discord.com/api/v10"  # 테스트는 .env의 DISCORD_API로 바꾼다
QUIET = {"allowed_mentions": {"parse": []}}  # 본문에 @everyone 같은 문자열이 있어도 멘션하지 않는다


def _chunks(text):
    while text:
        cut = text.rfind("\n", 0, LIMIT) if len(text) > LIMIT else len(text)
        cut = cut if cut > 0 else LIMIT
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def _request(url, payload=None, token=None, method=None):
    """payload가 있으면 POST(또는 method), 없으면 GET. token은 봇 토큰이다."""
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (ctfm, 1.0)"}
    if token:
        headers["Authorization"] = f"Bot {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
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


def forum_tags(env, channel):
    """포럼 채널의 태그 {이름(소문자): ID}. webhook은 태그 ID만 받고 목록은 주지 않아서 봇 토큰으로 채널 정보를 읽는다."""
    found = _request(f"{env.get('DISCORD_API', API)}/channels/{channel}", token=env["DISCORD_BOT_TOKEN"])["available_tags"]
    return {t["name"].lower(): t["id"] for t in found}


def new_post(store, env, url, title, category):
    """새 포스트 요청에 넣을 값. 포럼의 태그 중 이름이 분야와 같은 것(없으면 misc)을 붙인다.
    봇 토큰이 없거나 조회에 실패하면 태그 없이 만든다(알림은 잃지 않는다)."""
    post = {"thread_name": title[:100]}
    if env.get("DISCORD_BOT_TOKEN"):
        try:
            tags = forum_tags(env, _request(url)["channel_id"])
            tag = tags.get(category) or tags.get("misc")
            if tag:
                post["applied_tags"] = [tag]
        except (OSError, ValueError, KeyError) as e:
            store.emit("NOTIFY_ERROR", "포럼 태그 조회 실패, 태그 없이 포스트를 만듦 "
                                       f"(.env의 DISCORD_BOT_TOKEN과 봇의 채널 보기 권한 확인): {e}", echo=False)
    return post


def mark_solved(store, env, thread):
    """풀린 문제의 포스트에 solved 태그를 더하고 닫는다. 봇 토큰이 있을 때만 하고, 봇에 포스트 관리 권한이 필요하다.
    닫힌 포스트는 webhook 메시지가 오면 다시 열리므로 풀린 문제에 알림을 보낼 때마다 부른다(여러 번 해도 결과가 같다)."""
    if not env.get("DISCORD_BOT_TOKEN"):
        return
    try:
        post_url = f"{env.get('DISCORD_API', API)}/channels/{thread}"
        post = _request(post_url, token=env["DISCORD_BOT_TOKEN"])
        solved = forum_tags(env, post["parent_id"]).get("solved")
        tags = post.get("applied_tags", [])
        if solved and solved not in tags:
            tags = [*tags, solved]
        _request(post_url, {"applied_tags": tags, "archived": True}, env["DISCORD_BOT_TOKEN"], "PATCH")
    except (OSError, ValueError, KeyError) as e:
        store.emit("NOTIFY_ERROR", f"풀린 문제의 포스트에 solved 태그를 달고 닫지 못함 (봇의 포스트 관리 권한 확인): {e}", echo=False)


def discord(store, content, ch=None):
    """문제(ch)의 포스트에 보낸다. 없으면 `[분야] 문제이름`으로 만들고 분야 태그를 붙인다. 풀린 문제면 보낸 뒤 포스트를 닫는다.
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
                        _request(f"{url}?thread_id={thread}", {"content": chunk, **QUIET})
                        continue
                    except urllib.error.HTTPError as e:
                        if e.code not in (400, 404):  # 사용자가 포스트를 지웠으면 Unknown Channel: 새로 만든다
                            raise
                post = new_post(store, env, url, title, ch["category"] if ch else "misc")
                thread = _request(f"{url}?wait=true", {"content": chunk, **QUIET, **post})["channel_id"]
                with store.locked() as (cfg, st):
                    st.setdefault("threads", {})[key] = thread
            if ch and ch.get("status") == "solved" and thread:
                mark_solved(store, env, thread)
        return True
    except (OSError, ValueError, KeyError) as e:
        store.emit("NOTIFY_ERROR", f"Discord 전송 실패 (포럼 채널 webhook이어야 함): {e}", echo=False)
        return False
