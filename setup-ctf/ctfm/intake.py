"""문제 수집: 첨부 다운로드 → 압축 해제 → CHALLENGE.md / AGENTS.md / CLAUDE.md / Stop hook 생성."""
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .state import CTFM_BIN, TEMPLATES

# 배포본에 들어 있으면 에이전트 설정을 바꿔 버리는 이름들. 어느 깊이에 있든 `.dist`를 붙인다.
RESERVED = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude", ".mcp.json", ".codex", ".agents"}  # .agents/skills는 codex가 cwd에서 읽는다
# 우리가 문제 디렉토리 최상위에 만드는 파일들
OWN = {"CHALLENGE.md", "STATE.md", "BLOCKED.md"}
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")
NOTES_BEGIN, NOTES_END = "<!-- CTF_NOTES:BEGIN -->", "<!-- CTF_NOTES:END -->"


def extract(archive, dest):
    """확장자로 판단해 dest에 푼다. 아카이브가 아니면 False. 실패하면 예외."""
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:  # extract()가 절대 경로와 `..`를 제거한다
            for info in z.infolist():
                out = z.extract(info, dest)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    os.chmod(out, mode)  # zipfile은 실행 권한을 복원하지 않는다
    elif name.endswith(TAR_SUFFIXES):
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")  # 경로 탈출, 장치 파일, 외부 링크 거부
    elif name.endswith((".7z", ".rar")):
        subprocess.run(["7z", "x", "-y", f"-o{dest}", str(archive)], check=True, capture_output=True)
    else:
        return False
    return True


def _place(entry, target_dir):
    """entry를 target_dir로 옮긴다. 같은 이름이 있으면 `_`를 붙인다."""
    name = entry.name
    while (target_dir / name).exists() or (target_dir / name).is_symlink():
        name += "_"
    shutil.move(str(entry), str(target_dir / name))


def quarantine(chal_dir):
    """예약 이름을 `.dist`로 바꾸고 바뀐 경로 목록을 돌려준다. 우리 파일을 쓰기 전에 호출한다."""
    renamed = []
    for current, dirs, files in os.walk(chal_dir):
        blocked = RESERVED | OWN if Path(current) == chal_dir else RESERVED
        for name in [n for n in dirs + files if n in blocked]:
            path = Path(current) / name
            path.rename(path.with_name(name + ".dist"))
            renamed.append(str(path.relative_to(chal_dir)) + ".dist")
            if name in dirs:
                dirs.remove(name)
    return sorted(renamed)


def unpack(downloads, chal_dir, tmp_root):
    """받은 첨부들을 문제 디렉토리에 배치한다. [(파일명, 결과)] 반환."""
    archives = [f for f in downloads if f.name.lower().endswith((".zip", ".7z", ".rar") + TAR_SUFFIXES)]
    tmp_root.mkdir(parents=True, exist_ok=True)
    results = []
    for f in downloads:
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            if f not in archives:
                shutil.copy2(f, work / f.name)
                _place(work / f.name, chal_dir)
                results.append((f.name, "그대로 복사"))
                continue
            extract(f, work)
            entries = list(work.iterdir())
            if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
                entries = list(entries[0].iterdir())  # 최상위 디렉토리가 하나뿐이면 벗겨낸다
            target = chal_dir if len(archives) == 1 else chal_dir / f.name.split(".")[0]
            target.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                _place(entry, target)
            results.append((f.name, "압축 해제됨" + ("" if target == chal_dir else f" → {target.name}/")))
        except Exception as e:  # 암호 걸린 zip, 손상, 경로 탈출 시도 등
            shutil.copy2(f, work / f.name)
            _place(work / f.name, chal_dir)
            results.append((f.name, f"압축 해제 실패({type(e).__name__}: {e}), 원본을 그대로 둠"))
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results


def render(template, values):
    text = (TEMPLATES / template).read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def notes_text(root):
    path = root / "CTF_NOTES.md"
    return path.read_text().strip() if path.is_file() and path.read_text().strip() else "(아직 기록된 내용 없음)"


def write_agents(root, ch):
    chal_dir = root / ch["dir"]
    (chal_dir / "AGENTS.md").write_text(render("AGENTS.md", {
        "name": ch["name"], "category": ch["category"], "ctfm": CTFM_BIN,
        "notes": f"{NOTES_BEGIN}\n{notes_text(root)}\n{NOTES_END}",
    }))


def intake(store, http, adapter, ch):
    """문제 하나를 받아 풀이 에이전트가 바로 시작할 수 있는 디렉토리를 만든다."""
    detail = adapter.fetch(http, ch["id"])
    chal_dir = store.root / ch["dir"]
    marker = chal_dir / ".ctfm_incomplete"
    if marker.exists():
        shutil.rmtree(chal_dir)  # 이전 수집이 중간에 실패한 흔적
    elif chal_dir.exists():  # 사용자가 손으로 작업하던 디렉토리일 수 있으므로 건드리지 않는다
        raise FileExistsError(f"{ch['dir']} 가 이미 존재함. 옮기거나 지운 뒤 다시 수집됨")
    chal_dir.mkdir(parents=True)
    marker.touch()
    dl_dir = store.meta / "downloads" / str(ch["id"])
    dl_dir.mkdir(parents=True, exist_ok=True)
    downloads = []
    for f in detail["files"]:
        dest = dl_dir / Path(f["name"]).name
        http.download(f["url"], dest)
        downloads.append(dest)
    results = unpack(downloads, chal_dir, store.meta / "tmp")
    renamed = quarantine(chal_dir)

    def bullets(items, empty="없음"):
        return "\n".join(f"- {i}" for i in items) if items else empty

    (chal_dir / "CHALLENGE.md").write_text(render("CHALLENGE.md", {
        "name": ch["name"], "category": ch["category"], "site_category": ch["site_category"] or "없음",
        "points": ch["points"], "solves": ch["solves"],
        "connection_info": detail["connection_info"] or "별도 표기 없음 (설명 본문 확인)",
        "tags": ", ".join(detail["tags"]) or "없음",
        "max_attempts": f"{detail['max_attempts']}회" if detail["max_attempts"] else "표기 없음",
        "description": detail["description"].strip() or "(설명 없음)",
        "hints": bullets(detail["hints"]),
        "files": bullets([f"`{n}`: {r}" for n, r in results], "첨부 없음"),
        "renamed": bullets([f"`{r}`" for r in renamed]),
    }))
    write_agents(store.root, ch)
    (chal_dir / "CLAUDE.md").write_text("@AGENTS.md\n")
    (chal_dir / ".claude").mkdir()
    hook = {"type": "command", "command": f"{CTFM_BIN} hook stop", "timeout": 30}
    (chal_dir / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}, indent=1))
    marker.unlink()
