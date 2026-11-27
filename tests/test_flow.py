"""mock CTFd + 격리된 tmux 서버로 수집 → 스케줄링 → 보고 → 종료 흐름을 검증한다. 에이전트는 sleep 더미."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "setup-ctf"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ctfm import adapters, cli, daemon, state, tmux  # noqa: E402
from mock_ctfd import BOT_TOKEN, TOKEN, MockCTFd, make_zip  # noqa: E402

SOCKET = f"ctfm-test-{os.getpid()}"


@unittest.skipUnless(shutil.which("tmux"), "tmux 필요")
class Flow(unittest.TestCase):
    def setUp(self):
        os.environ.update(CTFM_TMUX_SOCKET=SOCKET, SHELL="/bin/sh")
        self.mock = MockCTFd()
        self.url = self.mock.start()
        self.root = Path(tempfile.mkdtemp()) / "TESTCTF"
        self.root.mkdir()
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.mock.add(1, "hard one", "Pwnable", value=500, solves=1, connection_info="nc 127.0.0.1 31337",
                      files={"hard.zip": make_zip({"hard/chall": (b"\x7fELF", 0o755), "hard/CLAUDE.md": (b"evil", 0o644)})},
                      description="heap 문제", max_attempts=5)
        self.mock.add(2, "easy one", "Pwnable", value=100, solves=50)
        self.mock.add(3, "mid web", "Web Exploitation", value=200, solves=20)
        self.mock.add(4, "wanted", "Cryptography", value=999, solves=0)
        self.mock.add(5, "done", "Misc", value=10, solves=99, solved=True)
        self.run_cli("init", "--url", self.url + "/challenges", "--token", TOKEN, "--max-agents", "2", "--priority", "wanted")
        state.write_env(self.root, {"DISCORD_WEBHOOK_URL": self.url + "/webhook"})
        self.store = state.Store(self.root)
        with self.store.locked() as (cfg, st):
            cfg.update(agent_cmd="sh -c 'sleep 600' agent", exit_wait=0)  # 더미는 /exit에 반응하지 않으므로 기다리지 않는다

    def tearDown(self):
        os.chdir(self.old_cwd)
        subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)
        self.mock.stop()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def run_cli(self, *argv, cwd=None, stdin=None):
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(cwd or old)
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin or "")
        try:
            with contextlib.redirect_stdout(out):
                code = cli.main(list(argv))
        finally:
            sys.stdin = old_stdin
            os.chdir(old)
        return code, out.getvalue()

    def poll(self):
        http, adapter = adapters.load(self.store)
        with contextlib.redirect_stdout(io.StringIO()):
            count = daemon.poll_once(self.store, http, adapter)
            daemon.housekeep(self.store)
            daemon.schedule(self.store)
        return count

    def status(self):
        return {c["name"]: c["status"] for c in self.store.state()["challenges"].values()}

    def chal_dir(self, name):
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == name)
        return self.root / ch["dir"]

    def pane_count(self, name):
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == name)
        return len(tmux.tmux("list-panes", "-t", ch["window_id"]).splitlines())

    def windows(self):
        out = subprocess.run(["tmux", "-L", SOCKET, "list-windows", "-a", "-F", "#{session_name}:#{window_name}"],
                             capture_output=True, text=True).stdout
        return sorted(out.split())

    def test_check(self):
        code, out = self.run_cli("check")
        result = json.loads(out)
        self.assertEqual((code, result["login_ok"], result["user"], result["challenges_visible"]), (0, True, "tester", 5))
        state.write_env(self.root, {"CTFD_TOKEN": "wrong"})
        code, out = self.run_cli("check")
        self.assertEqual((code, json.loads(out)["login_ok"]), (1, False))

    def test_not_started_then_full_flow(self):
        self.mock.started = False
        http, adapter = adapters.load(self.store)
        with self.assertRaises(Exception):
            daemon.poll_once(self.store, http, adapter)
        self.mock.started = True

        self.assertEqual(self.poll(), 5)
        # 우선순위 지정(wanted) → solve 수 많은 순(easy one). 이미 풀린 문제는 건너뜀.
        self.assertEqual(self.status(), {"hard one": "queued", "easy one": "running", "mid web": "queued",
                                         "wanted": "running", "done": "site_solved"})
        self.assertEqual(self.windows(), ["crypto_TESTCTF:wanted", "pwn_TESTCTF:easy_one"])
        self.assertEqual(self.pane_count("easy one"), 2)  # pwn은 codex와 claude를 좌우 pane에 동시에 (슬롯은 하나)
        self.assertEqual(self.pane_count("wanted"), 1)
        _, out = self.run_cli("status")
        self.assertIn("codex gpt-6-sol medium fast 재검토 0/2 | claude claude-opus-5 xhigh 재검토 0/2", out)
        _, out = self.run_cli("peek", "easy one")
        self.assertLess(out.index("── codex ──"), out.index("── claude ──"))

        # 디렉토리 구조와 생성 파일
        hard = self.chal_dir("hard one")
        self.assertEqual(hard, self.root / "pwn" / "hard_one")
        self.assertTrue(os.access(hard / "chall", os.X_OK))
        self.assertEqual((hard / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertEqual((hard / "CLAUDE.md.dist").read_bytes(), b"evil")
        challenge_md = (hard / "CHALLENGE.md").read_text()
        for expected in ("nc 127.0.0.1 31337", "heap 문제", "5회", "무료 힌트", "CLAUDE.md.dist", "Pwnable"):
            self.assertIn(expected, challenge_md)
        self.assertNotIn("token=secret", challenge_md)
        self.assertIn("포기는 없다", (hard / "AGENTS.md").read_text())
        self.assertIn("STATE.claude.md", (hard / "AGENTS.md").read_text())  # 동시 풀이 규칙은 pwn, rev에만
        self.assertNotIn("동시 풀이", (self.chal_dir("wanted") / "AGENTS.md").read_text())
        hooks = json.loads((hard / ".claude" / "settings.json").read_text())["hooks"]
        self.assertEqual(list(hooks), ["UserPromptSubmit"])  # 멈춤 방지는 Stop hook이 아니라 /goal이 맡는다
        self.assertTrue(hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("bin/ctfm hook prompt"))
        prompts = {f.name: f.read_text() for f in (self.root / ".ctfm" / "launch").glob("pwn-easy_one.*.prompt")}
        self.assertEqual(sorted(prompts), ["pwn-easy_one.claude.prompt", "pwn-easy_one.codex.prompt"])
        for text in prompts.values():  # 초기 프롬프트는 /goal 한 줄이고 완료 조건은 solved 보고다
            self.assertTrue(text.startswith("/goal ") and "\n" not in text and "ctfm report solved" in text and len(text) < 4000)
        self.assertIn("$solve-challenge", prompts["pwn-easy_one.codex.prompt"])
        self.assertIn("STATE.claude.md", prompts["pwn-easy_one.claude.prompt"])
        self.assertFalse(list(self.root.rglob(".env.*")) or (hard / ".env").exists())
        self.assertFalse((self.root / "misc" / "done").exists())

        # 폴링 중 새 문제: 슬롯이 가득이라 큐에만 들어감
        self.mock.add(6, "late ai", "AI", value=50, solves=80)
        self.poll()
        self.assertEqual(self.status()["late ai"], "queued")

        easy = self.chal_dir("easy one")
        hook_in = json.dumps({"cwd": str(easy)})
        _, out = self.run_cli("hook", "stop", stdin=hook_in)  # 예전 문제 디렉토리의 Stop hook 설정: 아무것도 하지 않는다
        self.assertEqual(out, "")

        # 재검토 2회 → 3번째 막힘은 blocked 안내, BLOCKED 파일 없이는 blocked 불가.
        # 동시 풀이에서 CTFM_AGENT가 없는 호출은 codex의 것이다(횟수는 pane별, 2회째는 codex도 effort 상향)
        _, out = self.run_cli("report", "retry", cwd=easy)
        self.assertIn("재검토 1/2 기록됨. 지금 다음을", out)
        _, out = self.run_cli("report", "retry", cwd=easy)
        self.assertIn("재검토 2/2 기록됨. 이번에는 effort를 max로", out)
        _, out = self.run_cli("report", "retry", cwd=easy)
        self.assertIn("ctfm report blocked", out)
        with self.assertRaises(SystemExit):
            self.run_cli("report", "blocked", cwd=easy)
        (easy / "BLOCKED.md").write_text("claude가 쓴 것")  # 상대의 파일은 보내지 않는다
        with self.assertRaises(SystemExit):
            self.run_cli("report", "blocked", cwd=easy)
        (easy / "BLOCKED.codex.md").write_text("## 막힌 지점\nlibc leak이 안 됨\n" + "상세 설명 " * 600)
        _, out = self.run_cli("report", "blocked", cwd=easy / ".claude")  # 하위 디렉토리에서 호출해도 문제를 찾는다
        self.assertIn("멈추지 말고", out)
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "easy one")
        self.assertEqual((ch["status"], ch["peer"]["retries"], ch["retries"], ch["blocked_count"]), ("blocked", 0, 0, 1))
        self.assertIn("(codex)", "".join(self.mock.webhook))
        blocked_msgs = [m for m in self.mock.webhook if "insight 요청" in m or "상세 설명" in m]
        self.assertGreater(len(blocked_msgs), 1)  # 2000자 초과분은 나눠서 전송
        self.assertTrue(all(len(m) <= 2000 for m in self.mock.webhook))
        self.assertIn("libc leak이 안 됨", "".join(self.mock.webhook))
        # 포럼 채널: 문제마다 포스트 하나에 모이고, `ctfm notify`는 대회 공통 포스트로 간다
        self.assertEqual([t["name"] for t in self.mock.threads.values()], ["[pwn] easy one"])
        self.assertEqual(len(self.mock.threads["1000"]["messages"]), len(self.mock.webhook))
        self.run_cli("notify", "관리 세션 공지")
        self.assertEqual(self.mock.threads["1001"], {"name": "TESTCTF", "messages": ["관리 세션 공지"]})
        del self.mock.threads["1001"]  # 사용자가 포스트를 지웠으면 새로 만든다
        self.assertEqual(self.run_cli("notify", "다시")[0], 0)
        self.assertEqual(self.mock.threads["1001"]["messages"], ["다시"])
        # blocked는 끝이 아님: 슬롯 유지
        self.poll()
        self.assertEqual(self.status()["late ai"], "queued")

        # ask: codex가 물은 것이면 claude는 계속 풀고, `ctfm send`로 답하면 running으로 돌아간다
        self.run_cli("report", "ask", "--question", "원격이 죽어 있음", cwd=easy)
        self.assertEqual(self.status()["easy one"], "waiting_user")
        self.run_cli("send", "--agent", "codex", "easy one", "다시 켰다")  # 옵션은 문제 이름 앞에
        self.assertEqual(self.status()["easy one"], "running")
        codex_screen, claude_screen = (tmux.capture(p["pane"]) for p in (ch["peer"], ch))
        self.assertIn("다시 켰다", codex_screen)
        self.assertNotIn("다시 켰다", claude_screen)

        # ask: claude가 물은 것이면 답이 들어올 때까지 waiting_user
        os.environ["CTFM_AGENT"] = "claude"
        self.addCleanup(os.environ.pop, "CTFM_AGENT", None)
        self.run_cli("report", "ask", "--question", "팀 토큰이 필요함", cwd=easy)
        self.assertEqual(self.status()["easy one"], "waiting_user")
        self.assertIn("팀 토큰이 필요함", self.mock.webhook[-1])
        self.assertIn("pwn_TESTCTF:?easy_one", self.windows())  # 윈도우 이름에 상태 표시
        self.assertEqual(self.status()["easy one"], "waiting_user")
        _, out = self.run_cli("hook", "prompt", stdin=hook_in)  # 사용자가 윈도우에 직접 답을 입력했다
        self.assertEqual((out, self.status()["easy one"]), ("", "running"))
        self.assertIn("pwn_TESTCTF:easy_one", self.windows())

        # solved: 알림, 슬롯 반환 → 대기 1순위(late ai, solves 80) spawn. 동시 풀이의 상대에게는 종료 메시지
        self.run_cli("report", "solved", "--flag", "flag{real}", cwd=easy)
        self.assertIn("flag{real}", self.mock.webhook[-1])
        self.assertIn("claude 세션이 이 문제의 플래그를 얻었다", tmux.capture(ch["peer"]["pane"]).replace("\n", ""))
        self.poll()
        self.assertEqual(self.status()["late ai"], "running")
        self.assertIn("ai_TESTCTF:late_ai", self.windows())
        self.assertIn("pwn_TESTCTF:✔easy_one", self.windows())  # 풀이 정리를 쓸 시간을 준 뒤에 닫는다
        with self.store.locked() as (cfg, st):
            st["challenges"][ch["id"]]["solved_at"] = time.time() - 60
        self.poll()
        self.assertNotIn("pwn_TESTCTF:✔easy_one", self.windows())  # 풀린 문제는 세션을 끝내고 윈도우를 지운다
        self.assertIn("CLOSED pwn/easy_one", self.store.events_path.read_text())

        # 사이트에서 풀림 표시: 실행 중이면 알림만, 대기 중이면 큐에서 제외
        self.mock.challenges[4]["solved_by_me"] = True
        self.mock.challenges[1]["solved_by_me"] = True
        self.poll()
        self.assertEqual((self.status()["wanted"], self.status()["hard one"]), ("running", "site_solved"))
        self.assertIn("SITE_SOLVED crypto/wanted", self.store.events_path.read_text())

        # kill: 윈도우 종료, 슬롯 반환 → 다음(mid web) spawn
        self.run_cli("kill", "wanted")
        self.poll()
        self.assertEqual((self.status()["wanted"], self.status()["mid web"]), ("killed", "running"))
        self.assertNotIn("crypto_TESTCTF:wanted", self.windows())

        # 한도를 넘는 즉시 시작, 메시지 주입
        self.run_cli("spawn", "wanted", "--now", "--resume")
        self.assertEqual(self.status()["wanted"], "running")
        self.assertEqual(sum(s in state.ACTIVE for s in self.status().values()), 3)
        self.run_cli("send", "wanted", "힌트: 첫 줄\n둘째 줄")

        # 문제 사이트에는 쓰기 요청이 한 번도 가지 않는다 (플래그 제출 포함)
        self.assertEqual((self.mock.writes, self.mock.attempts), ([], []))

    def test_exit_detection_and_prioritize(self):
        with self.store.locked() as (cfg, st):
            cfg["agent_cmd"] = "sh -c 'exit 3' agent"
            cfg["max_agents"] = 1
        self.poll()  # wanted가 먼저 뜨고 곧바로 종료된다
        deadline = time.time() + 10
        while time.time() < deadline and any(s in state.ACTIVE for s in self.status().values()):
            time.sleep(0.3)
            with contextlib.redirect_stdout(io.StringIO()):
                daemon.housekeep(self.store)
        self.assertEqual(self.status()["wanted"], "exited")
        self.assertIn("EXITED crypto/wanted", self.store.events_path.read_text())
        self.assertEqual(self.status()["hard one"], "queued")  # solve 수가 가장 적어 대기 순서 맨 뒤
        self.run_cli("prioritize", "hard")
        self.assertEqual(self.store.config()["priority"], ["hard one", "wanted"])
        with self.store.locked() as (cfg, st):
            cfg["agent_cmd"] = "sh -c 'sleep 600' agent"
        self.poll()
        self.assertEqual(self.status()["hard one"], "running")

    def test_existing_directory_is_not_overwritten(self):
        mine = self.root / "pwn" / "easy_one"
        mine.mkdir(parents=True)
        (mine / "my_work.py").write_text("keep")
        self.poll()
        self.assertEqual(self.status()["easy one"], "fetch_error")
        self.assertEqual((mine / "my_work.py").read_text(), "keep")

    def test_notes_sync(self):
        self.poll()
        (self.root / "CTF_NOTES.md").write_text("플래그 포맷: TEST{...}\n")
        self.run_cli("notes-sync")
        agents = (self.chal_dir("easy one") / "AGENTS.md").read_text()
        self.assertIn("플래그 포맷: TEST{...}", agents)
        self.assertNotIn("아직 기록된 내용 없음", agents)
        self.run_cli("notes-sync")  # 두 번 해도 구간이 망가지지 않는다
        self.assertEqual((self.chal_dir("easy one") / "AGENTS.md").read_text(), agents)

    def test_idle_nudge(self):
        """입력 대기로 멈춘 에이전트만 재개시키고, 작업 중이거나 질문 대기면 건드리지 않는다."""
        with self.store.locked() as (cfg, st):
            cfg.update(idle_nudge=1, max_agents=1, agent_cmd="sh -c 'echo ready; exec sleep 600' agent")
        self.poll()
        ch_id = next(i for i, c in self.store.state()["challenges"].items() if c["name"] == "wanted")

        def run_housekeep(seconds):
            end = time.time() + seconds
            while time.time() < end:
                daemon.housekeep(self.store)
                time.sleep(0.3)
            return self.store.state()["challenges"][ch_id]

        ch = run_housekeep(4)
        self.assertGreaterEqual(ch.get("nudges", 0), 1)
        self.assertIn("NUDGED", self.store.events_path.read_text())
        self.assertIn("플래그를 아직", tmux.capture(ch["window_id"], 40).replace("\n", ""))

        with self.store.locked() as (cfg, st):  # 질문 대기 중이면 건드리지 않는다
            st["challenges"][ch_id].update(status="waiting_user", nudges=0)
        self.assertEqual(run_housekeep(3).get("nudges"), 0)

        self.run_cli("kill", "wanted")  # 작업 중 표시가 있으면 화면이 멈춰 있어도 건드리지 않는다
        with self.store.locked() as (cfg, st):
            cfg["agent_cmd"] = "sh -c 'echo \"Working (esc to interrupt)\"; exec sleep 600' agent"
            st["challenges"][ch_id]["nudges"] = 0
        self.run_cli("spawn", "wanted", "--now")
        self.assertEqual(run_housekeep(3).get("nudges"), 0)


    def test_submit(self):
        """플래그 제출은 사용자가 허용했을 때(auto-submit 또는 --approved) 관리 세션만 한다."""
        self.poll()
        easy = self.chal_dir("easy one")
        self.run_cli("report", "solved", "--flag", "flag{wrong}", cwd=easy)
        with self.assertRaises(SystemExit):  # 허용 전에는 제출하지 않는다
            self.run_cli("submit", "easy one")
        with self.assertRaises(SystemExit):  # 풀이 에이전트(문제 디렉토리)는 허용돼 있어도 제출할 수 없다
            self.run_cli("submit", "easy one", "--approved", cwd=easy)
        self.assertEqual(self.mock.attempts, [])

        code, _ = self.run_cli("submit", "easy one", "--approved")  # 이번 한 번만 허용
        self.assertEqual((code, self.mock.attempts), (1, [(2, "flag{wrong}")]))
        self.assertIn("incorrect", self.mock.webhook[-1])
        with self.assertRaises(SystemExit):  # 같은 플래그를 다시 제출하지 않는다
            self.run_cli("submit", "easy one", "--approved")

        self.run_cli("set", "auto-submit", "1")  # 대회 전체 허용
        code, _ = self.run_cli("submit", "easy one", "--flag", "flag{real}")
        self.assertEqual((code, self.mock.attempts[-1]), (0, (2, "flag{real}")))
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "easy one")
        self.assertEqual(([s["status"] for s in ch["submissions"]], ch["site_solved"]), (["incorrect", "correct"], True))
        self.assertIn("SUBMITTED pwn/easy_one correct", self.store.events_path.read_text())
        self.assertEqual(self.mock.writes, [])

    def test_graceful_exit(self):
        """세션을 끝낼 때는 /exit를 보내고 에이전트가 정상 종료한 것을 확인한 뒤에 tmux 윈도우를 지운다."""
        reads_exit = "sh -c 'while read line; do case \"$line\" in */exit) exit 0;; esac; done' agent"
        with self.store.locked() as (cfg, st):
            cfg.update(max_agents=1, priority=["easy one"], exit_wait=10, agent_cmd=reads_exit)
        self.poll()
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "easy one")
        exit_files = [Path(ch["exit_file"]), Path(ch["peer"]["exit_file"])]
        self.assertFalse(any(f.exists() for f in exit_files))
        self.run_cli("kill", "easy one")
        self.assertEqual([f.read_text().strip() for f in exit_files], ["0", "0"])  # 두 pane 모두 스스로 끝났다
        self.assertNotIn("pwn_TESTCTF:easy_one", self.windows())
        self.assertNotIn("강제 종료", self.store.events_path.read_text())

        with self.store.locked() as (cfg, st):  # /exit에 반응하지 않으면 기다린 뒤 강제로 지우고 그 사실을 남긴다
            cfg.update(exit_wait=1, agent_cmd="sh -c 'sleep 600' agent")
        self.run_cli("spawn", "easy one", "--now")
        self.run_cli("kill", "easy one")
        self.assertNotIn("pwn_TESTCTF:easy_one", self.windows())
        self.assertIn("강제 종료", self.store.events_path.read_text())

    def test_forum_tags(self):
        """봇 토큰이 있으면 포럼의 태그 목록을 읽어 포스트를 만들 때 분야 태그를 붙인다. 없거나 조회에 실패해도 알림은 간다."""
        self.mock.tags = {"pwn": "111", "Crypto": "222", "misc": "333"}
        self.poll()
        easy, wanted = self.chal_dir("easy one"), self.chal_dir("wanted")
        tags = lambda: {t["name"]: t.get("tags") for t in self.mock.threads.values()}
        self.run_cli("notify", "토큰 없음")
        self.assertEqual(tags(), {"TESTCTF": None})

        state.write_env(self.root, {"DISCORD_API": self.url + "/discord", "DISCORD_BOT_TOKEN": "wrong"})
        self.run_cli("report", "ask", "--question", "토큰 필요", cwd=wanted)
        self.assertEqual(tags()["[crypto] wanted"], None)  # 조회 실패: 태그 없이 만들고 원인을 남긴다
        self.assertIn("포럼 태그 조회 실패", self.store.events_path.read_text())

        state.write_env(self.root, {"DISCORD_BOT_TOKEN": BOT_TOKEN})
        self.mock.threads.clear()  # 포스트가 지워졌으면 다시 만든다. 이번에는 태그가 붙는다
        self.run_cli("report", "ask", "--question", "다시", cwd=wanted)
        self.assertEqual(tags(), {"[crypto] wanted": ["222"]})  # 태그 이름은 대소문자를 가리지 않는다
        del self.mock.tags["pwn"]
        self.run_cli("report", "ask", "--question", "원격 다운", cwd=easy)
        self.assertEqual(tags()["[pwn] easy one"], ["333"])  # 분야 태그가 없으면 misc

    def test_effort_escalation(self):
        """재검토 2회째: claude가 멈춰 유휴가 되면 /effort max → /compact → 엄밀한 재검토 지시."""
        with self.store.locked() as (cfg, st):
            cfg.update(idle_nudge=0, max_agents=1, agent_cmd="sh -c 'echo ready; exec sleep 600' agent")
        self.poll()
        wanted = self.chal_dir("wanted")
        hook_in = json.dumps({"cwd": str(wanted)})
        _, out = self.run_cli("report", "retry", cwd=wanted)
        self.assertIn("재검토 1/2 기록됨. 지금 다음을", out)
        _, out = self.run_cli("report", "retry", cwd=wanted)
        self.assertIn("effort를 max로", out)
        self.assertIn("여기서 멈추고 기다린다", out)

        ch = None
        deadline = time.time() + 20
        while time.time() < deadline:
            daemon.housekeep(self.store)
            ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "wanted")
            if "escalate" not in ch:
                break
            time.sleep(0.5)
        self.assertEqual((ch.get("escalate"), ch["effort"]), (None, "max"))
        screen = tmux.capture(ch["pane"], 60).replace("\n", "")
        for expected in ("/effort max", "/compact", "처음부터 엄밀하게 다시 본다"):
            self.assertIn(expected, screen)
        self.assertLess(screen.index("/effort max"), screen.index("/compact"))
        self.assertIn("ESCALATED crypto/wanted", self.store.events_path.read_text())
        _, out = self.run_cli("report", "retry", cwd=wanted)  # 3번째 막힘: 사용자 알림 단계
        self.assertIn("ctfm report blocked", out)

        # 알림 뒤 다음 사이클에서는 이미 max라 다시 올리지 않는다
        (wanted / "BLOCKED.md").write_text("막힘")
        self.run_cli("report", "blocked", cwd=wanted)
        self.run_cli("report", "retry", cwd=wanted)
        _, out = self.run_cli("report", "retry", cwd=wanted)
        self.assertIn("재검토 2/2 기록됨. 지금 다음을", out)

    def test_effort_escalation_codex(self):
        """codex도 같다: /model 메뉴로 effort max → /compact → (세워 둔 목표가 있으면 /goal resume) → 엄밀한 재검토 지시."""
        stopped = "sh -c 'echo \"Goal paused (/goal resume)\"; exec sleep 600' agent"
        with self.store.locked() as (cfg, st):
            cfg.update(idle_nudge=0, max_agents=1, priority=["easy one"], agent_cmd=stopped)
        self.poll()
        easy = self.chal_dir("easy one")
        self.run_cli("report", "retry", cwd=easy)  # CTFM_AGENT가 없는 호출은 codex의 것이다
        _, out = self.run_cli("report", "retry", cwd=easy)
        self.assertIn("여기서 멈추고 기다린다", out)

        ch = None
        deadline = time.time() + 30
        while time.time() < deadline:
            daemon.housekeep(self.store)
            ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "easy one")
            if "escalate" not in ch["peer"]:
                break
            time.sleep(0.5)
        self.assertEqual((ch["peer"].get("escalate"), ch["peer"]["effort"], ch.get("effort")), (None, "max", None))
        screen = tmux.capture(ch["peer"]["pane"], 60).replace("\n", "")
        # 가짜 에이전트가 찍은 하단 표시에도 "/goal resume"이 있어 마지막 위치(입력된 것)를 본다
        steps = [screen.rindex(s) for s in ("/model", "/compact", "/goal resume", "처음부터 엄밀하게 다시 본다")]
        self.assertEqual(steps, sorted(steps))
        self.assertNotIn("/compact", tmux.capture(ch["pane"], 60))  # claude pane은 그대로다
        self.assertIn("ESCALATED pwn/easy_one[codex]", self.store.events_path.read_text())
        self.assertIn("codex gpt-6-sol max", self.run_cli("status")[1])

        # `ctfm send`와 유휴 재개도 멈춘 codex 목표를 먼저 잇는다. claude에는 넣지 않는다
        resumes = lambda p: tmux.capture(p["pane"], 60).replace("\n", "").count("/goal resume")
        before = resumes(ch["peer"])
        self.run_cli("send", "easy one", "힌트다")
        self.assertEqual((resumes(ch["peer"]), resumes(ch)), (before + 1, 1))  # 1은 가짜 에이전트가 찍은 하단 표시
        with self.store.locked() as (cfg, st):
            cfg["idle_nudge"] = 1
        deadline = time.time() + 15
        while time.time() < deadline and "NUDGED pwn/easy_one[codex]" not in self.store.events_path.read_text():
            daemon.housekeep(self.store)
            time.sleep(0.3)
        self.assertGreaterEqual(resumes(ch["peer"]), before + 2)

    def test_dual_exit(self):
        """동시 풀이는 한쪽이 끝나도 슬롯을 유지하고, 둘 다 끝나야 exited가 된다."""
        with self.store.locked() as (cfg, st):
            cfg.update(max_agents=1, priority=["easy one"], agent_cmd="sh -c 'exit 3' agent")
        self.poll()
        ch = next(c for c in self.store.state()["challenges"].values() if c["name"] == "easy one")
        self.assertEqual((ch["agent"], ch["peer"]["agent"]), ("claude", "codex"))
        self.assertNotEqual(ch["exit_file"], ch["peer"]["exit_file"])
        deadline = time.time() + 10
        while time.time() < deadline and self.status()["easy one"] in state.ACTIVE:
            time.sleep(0.3)
            with contextlib.redirect_stdout(io.StringIO()):
                daemon.housekeep(self.store)
        self.assertEqual(self.status()["easy one"], "exited")
        events = self.store.events_path.read_text()
        self.assertIn("EXITED pwn/easy_one[codex]", events)
        self.assertIn("EXITED pwn/easy_one[claude]", events)

    def test_koth(self):
        """KotH: 분류, 최우선 spawn, 사이트 접근 위임, 모델 체인 자동 전환, 라운드에 따른 fast 조정."""
        self.mock.add(9, "Hill Climb", "King of the Hill", value=1000, solves=0)
        # 사용량 한도 문구를 띄우고 멈추는 더미 에이전트
        with self.store.locked() as (cfg, st):
            cfg.update(max_agents=1, idle_nudge=0, agent_cmd="sh -c 'echo \"You have hit your usage limit.\"; exec sleep 600' agent")
        http, adapter = adapters.load(self.store)
        with contextlib.redirect_stdout(io.StringIO()):
            daemon.poll_once(self.store, http, adapter)  # housekeep 없이 수집과 spawn만
        chals = self.store.state()["challenges"]
        hill = chals["9"]
        self.assertEqual((hill["category"], hill["dir"], hill["status"]), ("koth", "koth/Hill_Climb", "running"))
        self.assertEqual((hill["agent"], hill["model"], hill["chain_idx"]), ("codex", "gpt-6.2-astra", 0))
        self.assertEqual(chals["4"]["status"], "queued")  # --priority로 지정한 wanted보다 앞

        chal = self.chal_dir("Hill Climb")
        access = chal / "CTF_ACCESS.env"
        self.assertEqual(access.stat().st_mode & 0o777, 0o600)
        text = access.read_text()
        self.assertIn(f"CTFD_TOKEN={TOKEN}", text)
        self.assertIn("CHALLENGE_ID=9", text)
        self.assertNotIn("DISCORD_WEBHOOK_URL", text)
        self.assertIn("King of the Hill", (chal / "AGENTS.md").read_text())
        self.assertIn("CTF_ACCESS.env", (chal / "AGENTS.md").read_text())
        self.assertIn("상대 전략 분석 subagent", (chal / "AGENTS.md").read_text())
        self.assertEqual((chal / "CLAUDE.md").read_text(), "@AGENTS.md\n")  # claude는 CLAUDE.md, codex는 AGENTS.md를 읽는다

        def watch_until(pred, seconds=8):
            end = time.time() + seconds
            while time.time() < end:
                with self.store.locked() as (cfg, st):  # 확인 간격을 기다리지 않게
                    st["challenges"]["9"]["idle_checked"] = 0
                daemon.housekeep(self.store)
                if pred(self.store.state()["challenges"]["9"]):
                    break
                time.sleep(0.5)
            return self.store.state()["challenges"]["9"]

        hill = watch_until(lambda c: c.get("chain_idx") == 1)
        self.assertEqual((hill["agent"], hill["model"]), ("claude", "claude-mythos-5-2"))
        hill = watch_until(lambda c: c.get("chain_idx") == 2)
        self.assertEqual(hill["model"], "claude-opus-5-5")
        hill = watch_until(lambda c: c.get("fallback_exhausted"))
        self.assertTrue(hill.get("fallback_exhausted"))
        events = self.store.events_path.read_text()
        self.assertEqual(events.count("MODEL_FALLBACK"), 2)
        self.assertIn("MODEL_UNAVAILABLE", events)
        self.assertEqual(hill["status"], "running")  # 슬롯은 유지

        # fast: 라운드 3까지 켬, 이후 끔. 하단 표시를 흉내 내는 더미로 확인한다
        self.run_cli("kill", "Hill Climb")
        with self.store.locked() as (cfg, st):
            cfg["agent_cmd"] = "sh -c 'echo \"  GPT-6.2-Astra high · /x · Ready\"; exec sleep 600' agent"
            st["challenges"]["9"].update(chain_idx=0, agent="codex", model="gpt-6.2-astra")
        self.run_cli("spawn", "Hill Climb", "--now")
        hill = watch_until(lambda c: c.get("fast_sent"))
        self.assertIn("fast 켬", self.store.events_path.read_text())
        self.assertIn("/fast", tmux.capture(hill["window_id"], 40))

        self.run_cli("report", "round", "--round", "3", "--summary", "R2 3위", cwd=chal)
        hill = watch_until(lambda c: c.get("peer"))  # 라운드 3: 후임을 미리 띄운다. fast는 라운드 4까지 유지
        self.assertEqual(hill["peer"]["role"], "successor")
        self.assertNotIn("fast 끔", self.store.events_path.read_text())
        self.run_cli("report", "round", "--round", "5", "--summary", "R4 2위(+120)\nR5: 탐색 깊이 3으로 제출 예정", cwd=chal)
        self.assertIn("라운드 5\nR4 2위(+120)", self.mock.webhook[-1])  # 라운드마다 진행 요약이 그 문제의 포스트로 간다
        self.assertEqual([t["name"] for t in self.mock.threads.values()], ["[koth] Hill Climb"])
        with self.assertRaises(SystemExit):
            self.run_cli("report", "round", "--round", "6", cwd=chal)
        self.run_cli("kill", "Hill Climb")
        with self.store.locked() as (cfg, st):
            cfg["agent_cmd"] = "sh -c 'echo \"  GPT-6.2-Astra high fast · /x · Ready\"; exec sleep 600' agent"
            st["challenges"]["9"].update(chain_idx=0, agent="codex", model="gpt-6.2-astra", fast_sent=0)
        self.run_cli("spawn", "Hill Climb", "--now")
        hill = watch_until(lambda c: c.get("fast_sent") and c.get("peer"))
        self.assertIn("fast 끔 (라운드 5", self.store.events_path.read_text())

        # 전환 준비: 체인의 다음 모델이 후임으로 오른쪽 pane에 떠 있다
        peer = hill["peer"]
        self.assertEqual((peer["agent"], peer["model"], peer["effort"], peer["role"]), ("claude", "claude-mythos-5-2", "high", "successor"))
        self.assertEqual(self.pane_count("Hill Climb"), 2)
        self.assertIn("SUCCESSOR koth/Hill_Climb", self.store.events_path.read_text())
        hook_in = json.dumps({"cwd": str(chal)})
        with self.assertRaises(SystemExit):  # 메인(codex)은 ready를 보고할 수 없다
            self.run_cli("report", "ready", "--summary", "x", cwd=chal)
        os.environ["CTFM_AGENT"] = "claude"
        self.addCleanup(os.environ.pop, "CTFM_AGENT", None)
        with self.assertRaises(SystemExit):  # 후임은 라운드를 보고하지 않는다
            self.run_cli("report", "round", "--round", "6", "--summary", "x", cwd=chal)
        self.run_cli("report", "ready", "--summary", "기준 후보 로컬 재현 완료", cwd=chal)
        self.assertIn("기준 후보 로컬 재현 완료", self.mock.webhook[-1])
        self.assertIn("READY koth/Hill_Climb[claude]", self.store.events_path.read_text())
        os.environ.pop("CTFM_AGENT")

        # 전환: 후임이 메인이 되고 Astra pane은 종료된다
        self.run_cli("handover", "Hill Climb")
        hill = self.store.state()["challenges"]["9"]
        self.assertEqual((hill["agent"], hill["model"], hill["effort"], hill["chain_idx"]), ("claude", "claude-mythos-5-2", "high", 1))
        self.assertNotIn("peer", hill)
        self.assertNotIn("role", hill)
        self.assertEqual(self.pane_count("Hill Climb"), 1)
        screen = tmux.capture(hill["pane"], 60).replace("\n", "")  # 전환 메시지는 메인 목표로 바꾸는 /goal이다
        self.assertIn("/goal 이 King of the Hill 문제의 메인 에이전트로서", screen)
        self.assertIn("이전 메인(codex) 세션은 종료됐고", screen)
        self.assertIn("HANDOVER koth/Hill_Climb gpt-6.2-astra → claude-mythos-5-2", self.store.events_path.read_text())
        self.assertIn("메인 전환", self.mock.webhook[-1])
        with self.assertRaises(SystemExit):
            self.run_cli("handover", "Hill Climb")



if __name__ == "__main__":
    unittest.main()
