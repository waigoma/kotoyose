"""同梱シェルスクリプトの構文検査。

deploy.sh は本番の再起動を握る。構文誤りを push すると、実行するまで気づけない。
shellcheck は環境に無いことがあるため、必ず動く `bash -n` を土台にする。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(ROOT.glob("scripts/**/*.sh"))


def test_scripts_are_discovered():
    """glob が空振りしたまま「全部通った」ことにならないように。"""
    source_workflow = ROOT / ".github/workflows/release-public.yml"
    export_manifest = ROOT / "release/public-paths.txt"
    if not SCRIPTS and export_manifest.is_file() and not source_workflow.is_file():
        pytest.skip("deployment scripts are outside the public export")
    assert SCRIPTS, "scripts/ 配下に *.sh が見つかりません"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax_is_valid(script: Path):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"{script.name}:\n{result.stderr}"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck 未導入")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck_reports_no_error(script: Path):
    """error 級のみを見る (style / info は既存コードの粒度に合わせない)。"""
    result = subprocess.run(
        ["shellcheck", "--severity=error", "--format=gcc", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script.name}:\n{result.stdout}"
