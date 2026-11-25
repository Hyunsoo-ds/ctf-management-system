"""tmux 조작. 윈도우는 번호가 아니라 window_id(@N)로 추적한다(renumber-windows 대응)."""
import os
import re
import shlex
import subprocess
import time

# 관리 세션(Claude Code)에서 물려받으면 풀이 세션이 자식 세션처럼 동작하게 되는 변수들
SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_EFFORT",
)


def tmux(*args, stdin=None, check=True):
    cmd = ["tmux"]
    if os.environ.get("CTFM_TMUX_SOCKET"):  # 테스트 격리용
        cmd += ["-L", os.environ["CTFM_TMUX_SOCKET"]]
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    result = subprocess.run([*cmd, *args], input=stdin, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args[:2])} 실패: {result.stderr.strip()}")
    return result.stdout.strip()


def session_name(root, category):
    ctf = re.sub(r"[.:\s]", "_", root.name)  # tmux 세션 이름에 쓸 수 없는 문자 제거
    return f"{ctf}-{category}"


def spawn_window(session, name, cwd, command):
    """session에 윈도우를 만들어 셸 명령 command를 실행하고, 끝난 뒤에도 셸을 남긴다. window_id를 돌려준다."""
    shell = os.environ.get("SHELL", "/bin/sh")
    inner = f"{command}; exec {shell}"
    command = f"{shell} -ic {shlex.quote(inner)}"
    common = ["-d", "-P", "-F", "#{window_id}", "-n", name, "-c", str(cwd)]
    if session in tmux("list-sessions", "-F", "#{session_name}", check=False).splitlines():
        return tmux("new-window", *common, "-t", f"={session}:", command)
    return tmux("new-session", *common, "-s", session, "-x", "200", "-y", "50", command)


MARKS = {"solved": "✔", "blocked": "!", "waiting_user": "?"}


def mark(ch):
    """윈도우 이름 앞에 상태를 표시해 tmux 상태줄과 choose-tree에서 바로 보이게 한다."""
    if ch.get("window_id"):
        tmux("rename-window", "-t", ch["window_id"], MARKS.get(ch["status"], "") + ch["slug"], check=False)


def alive_windows():
    return set(tmux("list-windows", "-a", "-F", "#{window_id}", check=False).split())


def kill_window(window_id):
    tmux("kill-window", "-t", window_id, check=False)


def send_text(window_id, text):
    """여러 줄 텍스트를 붙여넣고 Enter를 누른다."""
    tmux("load-buffer", "-b", "ctfm-send", "-", stdin=text)
    tmux("paste-buffer", "-p", "-d", "-b", "ctfm-send", "-t", window_id)
    time.sleep(0.3)
    tmux("send-keys", "-t", window_id, "Enter")


def being_viewed(window_id):
    """사용자가 이 윈도우를 화면에 띄워 보고 있는지 (세션에 붙어 있고 이 윈도우가 활성)."""
    out = tmux("display-message", "-p", "-t", window_id, "#{session_attached}:#{window_active}", check=False).strip()
    attached, _, active = out.partition(":")
    return attached not in ("", "0") and active == "1"


def capture(window_id, lines=40):
    out = tmux("capture-pane", "-p", "-t", window_id, check=False)
    return "\n".join([l for l in out.splitlines() if l.strip()][-lines:])
