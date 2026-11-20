"""agent_container_launcher の argv 組み立てと設定検証を検証する。

os.execvp は patch し、実際には置き換えない。secret の値が argv に出ないことが要点。
"""

from __future__ import annotations

import ast
import errno
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kotoyose import agent_container as ac
from kotoyose import agent_container_launcher as launcher


SECRET_VALUE = "sk\x2dlive-do-not-print-987"
GITHUB_VALUE = "ghp_do-not-print-654"


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "KOTOYOSE_AGENT_CONTAINER": "kotoyose-agent-job-1",
        "KOTOYOSE_AGENT_BINARY": "/opt/agent/bin/claude",
        "KOTOYOSE_AGENT_SECRET_ENV": "ANTHROPIC_API_KEY,GH_TOKEN",
        "KOTOYOSE_AGENT_ENV": json.dumps({"KOTOYOSE_MODE": "fast", "LANG": "C.UTF-8"}),
        "KOTOYOSE_DOCKER_BINARY": "/usr/bin/docker",
        "ANTHROPIC_API_KEY": SECRET_VALUE,
        "GH_TOKEN": GITHUB_VALUE,
        "PATH": "/usr/bin",
    }
    env.update(overrides)
    return env


def test_builds_exact_docker_exec_argv_without_secret_values():
    env = _base_env()
    with patch.object(launcher.os, "execvp") as execvp:
        code = launcher.main(argv=["--print", "--output-format", "stream-json"], environ=env)

    assert code == 0
    execvp.assert_called_once()
    program, argv = execvp.call_args.args
    assert program == "/usr/bin/docker"
    assert argv == [
        "/usr/bin/docker", "exec", "-i",
        "-e", "KOTOYOSE_MODE=fast",
        "-e", "LANG=C.UTF-8",
        "-e", "ANTHROPIC_API_KEY",
        "-e", "GH_TOKEN",
        "kotoyose-agent-job-1",
        "/opt/agent/bin/claude",
        "--print", "--output-format", "stream-json",
    ]
    joined = "\0".join(argv)
    assert SECRET_VALUE not in joined
    assert GITHUB_VALUE not in joined


def test_default_docker_binary_and_no_secret_env_gives_minimal_argv():
    env = {"KOTOYOSE_AGENT_CONTAINER": "kotoyose-agent-j", "KOTOYOSE_AGENT_BINARY": "/opt/agent/bin/codex"}
    with patch.object(launcher.os, "execvp") as execvp:
        assert launcher.main(argv=[], environ=env) == 0
    assert execvp.call_args.args[1] == ["docker", "exec", "-i", "kotoyose-agent-j", "/opt/agent/bin/codex"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"KOTOYOSE_AGENT_CONTAINER": "not-an-agent-container"},
        {"KOTOYOSE_AGENT_CONTAINER": "kotoyose-agent-bad/job"},
        {"KOTOYOSE_AGENT_BINARY": "relative/claude"},
        {"KOTOYOSE_AGENT_BINARY": "/opt/agent/bin/claude\n--x"},
        {"KOTOYOSE_DOCKER_BINARY": "-H"},
        {"KOTOYOSE_AGENT_SECRET_ENV": "NOT_SET_ANYWHERE"},
        {"KOTOYOSE_AGENT_SECRET_ENV": "BAD-NAME"},
        {"KOTOYOSE_AGENT_ENV": "{not json"},
        {"KOTOYOSE_AGENT_ENV": json.dumps(["A", "B"])},
        {"KOTOYOSE_AGENT_ENV": json.dumps({"OPENAI_API_KEY": SECRET_VALUE})},
        {"KOTOYOSE_AGENT_ENV": json.dumps({"LANG": 1})},
        {"KOTOYOSE_AGENT_ENV": json.dumps({"ANTHROPIC_API_KEY": "x"}), "KOTOYOSE_AGENT_SECRET_ENV": "ANTHROPIC_API_KEY"},
    ],
)
def test_bad_config_exits_78_without_echoing_values(overrides, capsys):
    env = _base_env(**overrides)
    with patch.object(launcher.os, "execvp") as execvp:
        code = launcher.main(argv=[], environ=env)

    assert code == 78
    execvp.assert_not_called()
    err = capsys.readouterr().err
    assert err.startswith("agent launcher: ")
    assert SECRET_VALUE not in err
    assert GITHUB_VALUE not in err
    assert "{not json" not in err


def test_missing_docker_binary_returns_127(capsys):
    with patch.object(launcher.os, "execvp", side_effect=FileNotFoundError(errno.ENOENT, "docker")):
        code = launcher.main(argv=[], environ=_base_env())
    assert code == 127
    assert SECRET_VALUE not in capsys.readouterr().err


def test_other_exec_failure_returns_126(capsys):
    with patch.object(launcher.os, "execvp", side_effect=PermissionError(errno.EACCES, "docker")):
        code = launcher.main(argv=[], environ=_base_env())
    assert code == 126
    assert SECRET_VALUE not in capsys.readouterr().err


@pytest.mark.parametrize("raw", ["[" * 100000 + "]" * 100000, "{not json", "[1,2"])
def test_json_failures_including_recursion_exit_78(raw, capsys):
    with patch.object(launcher.os, "execvp") as execvp:
        code = launcher.main(argv=[], environ=_base_env(KOTOYOSE_AGENT_ENV=raw))
    assert code == 78
    execvp.assert_not_called()
    assert raw[:5] not in capsys.readouterr().err


@pytest.mark.parametrize("name", ["PATH", "DOCKER_HOST", "DOCKER_CONFIG"])
def test_reserved_secret_names_are_rejected(name, capsys):
    env = _base_env(KOTOYOSE_AGENT_SECRET_ENV=name, **{name: "x"})
    with patch.object(launcher.os, "execvp") as execvp:
        assert launcher.main(argv=[], environ=env) == 78
    execvp.assert_not_called()


def test_launcher_duplicates_match_agent_container_definitions():
    assert launcher.CONTAINER_PREFIX == ac.CONTAINER_PREFIX
    assert launcher.JOB_ID_RE.pattern == ac.JOB_ID_RE.pattern
    assert launcher.ENV_NAME_RE.pattern == ac.ENV_NAME_RE.pattern
    assert launcher._SECRET_NAME_RE.pattern == ac._SECRET_NAME_RE.pattern
    assert launcher._SECRET_NAME_RE.flags == ac._SECRET_NAME_RE.flags
    assert launcher._RESERVED_SECRET_NAMES == ac._RESERVED_SECRET_NAMES
    assert launcher._RESERVED_SECRET_PREFIX == ac._RESERVED_SECRET_PREFIX
    samples = ["ANTHROPIC_API_KEY", "github_token", "PATH", "DB_DSN", "LANG", "HOME", "x_passwd", "PRIVATE_X"]
    assert [launcher.is_secret_env_name(n) for n in samples] == [ac.is_secret_env_name(n) for n in samples]


def test_launcher_is_stdlib_only():
    source = Path(launcher.__file__).read_text(encoding="utf-8")
    stdlib = {"__future__", "errno", "json", "os", "re", "sys", "collections"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [(node.module or "").split(".")[0]] if node.level == 0 else ["relative"]
        else:
            continue
        assert set(modules) <= stdlib, modules


def test_build_exec_argv_is_pure_and_deduplicates_secret_names():
    env = _base_env(KOTOYOSE_AGENT_SECRET_ENV="GH_TOKEN, ,GH_TOKEN")
    argv = launcher.build_exec_argv(env, [])
    assert argv.count("GH_TOKEN") == 1
    assert "ANTHROPIC_API_KEY" not in argv


def test_relay_socket_wraps_the_cli_in_the_container_relay():
    env = _base_env(KOTOYOSE_AGENT_RELAY_SOCKET="/run/egress/egress.sock", KOTOYOSE_AGENT_WORKDIR="/workspace")
    argv = launcher.build_exec_argv(env, ["-p", "--output-format", "stream-json"])
    assert argv == [
        "/usr/bin/docker", "exec", "-i",
        "--workdir", "/workspace",
        "-e", "KOTOYOSE_MODE=fast",
        "-e", "LANG=C.UTF-8",
        "-e", "ANTHROPIC_API_KEY",
        "-e", "GH_TOKEN",
        "kotoyose-agent-job-1",
        "python3", "/opt/agent/egress_relay.py", "/run/egress/egress.sock", "--",
        "/opt/agent/bin/claude", "-p", "--output-format", "stream-json",
    ]
    joined = "\0".join(argv)
    assert SECRET_VALUE not in joined and GITHUB_VALUE not in joined


def test_relay_is_not_used_without_a_socket():
    argv = launcher.build_exec_argv(_base_env(), [])
    assert "python3" not in argv and "--workdir" not in argv
    assert argv[-1] == "/opt/agent/bin/claude"


@pytest.mark.parametrize("name", ["KOTOYOSE_AGENT_RELAY_SOCKET", "KOTOYOSE_AGENT_WORKDIR"])
@pytest.mark.parametrize("value", ["relative/path", "/run/egress\nx", "/run/egress\x00x"])
def test_relay_and_workdir_must_be_absolute_container_paths(name, value):
    with pytest.raises(launcher.LauncherConfigError):
        launcher.build_exec_argv(_base_env(**{name: value}), [])
