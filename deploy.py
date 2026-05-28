"""
GitHub Pages デプロイスクリプト
output/report.html を docs/index.html にコピーして git push する
"""

import subprocess
import shutil
import sys
import io
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
REPORT_SRC = BASE_DIR / "output" / "report.html"
DOCS_DIR   = BASE_DIR / "docs"
INDEX_DST  = DOCS_DIR / "index.html"


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def deploy() -> bool:
    if not REPORT_SRC.exists():
        print(f"[deploy] レポートが見つかりません: {REPORT_SRC}")
        return False

    DOCS_DIR.mkdir(exist_ok=True)
    shutil.copy(REPORT_SRC, INDEX_DST)
    print(f"[deploy] docs/index.html にコピー完了")

    # docs と data の両方をコミット対象に追加
    git("add", "docs/index.html")
    git("add", "data/properties.json")
    for optional_file in ["data/status.json", "data/notes.json"]:
        if (BASE_DIR / optional_file).exists():
            git("add", optional_file)

    # 変更がなければスキップ
    diff = git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        print("[deploy] 変更なし、スキップ")
        return True

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = git("commit", "-m", f"report: {today}")
    if commit.returncode != 0:
        print(f"[deploy] commit 失敗: {commit.stderr.strip()}")
        return False

    push = git("push", "origin", "main")
    if push.returncode == 0:
        print("[deploy] GitHub Pages へのデプロイ完了")
        return True
    else:
        print(f"[deploy] push 失敗: {push.stderr.strip()}")
        return False


if __name__ == "__main__":
    success = deploy()
    sys.exit(0 if success else 1)
