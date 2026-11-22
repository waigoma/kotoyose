"""Workflow files stay pinned, and release jobs keep their permission split."""

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
CHECKOUT = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
# release-public.yml lives only in the private repository; the public snapshot runs these tests too.
RELEASE_PUBLIC = WORKFLOWS / "release-public.yml"
private_only = pytest.mark.skipif(not RELEASE_PUBLIC.exists(), reason="private-only workflow")


def load_workflow(path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML 1.1 reads the workflow key `on` as boolean True.
    if True in document and "on" not in document:
        document["on"] = document.pop(True)
    return document


def uses_targets(text):
    found = []
    for line in text.splitlines():
        code = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"(?:-\s*)?uses:\s*(\S+)", code)
        if match:
            found.append(match.group(1))
    return found


def test_workflow_uses_are_pinned_to_a_commit_sha():
    """Actions use a 40-hex commit. docker:// steps use a sha256 image digest."""
    workflows = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert [path.name for path in workflows]
    if RELEASE_PUBLIC.exists():
        assert CHECKOUT in RELEASE_PUBLIC.read_text(encoding="utf-8")
    seen = 0
    for path in workflows:
        targets = uses_targets(path.read_text(encoding="utf-8"))
        if path.name == "github-release.yml":
            assert targets == [CHECKOUT]
        for target in targets:
            seen += 1
            if target.startswith("docker://"):
                assert re.fullmatch(r"docker://.+@sha256:[0-9a-f]{64}", target), target
            else:
                assert re.fullmatch(r".+@[0-9a-f]{40}", target), target
    assert seen >= 1


@private_only
def test_private_tag_job_writes_only_with_its_own_token():
    document = load_workflow(RELEASE_PUBLIC)
    assert document["permissions"] == {"contents": "read"}
    jobs = document["jobs"]
    job = jobs["tag-private"]
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert set(needs) == {"build", "publish"}
    assert "needs.publish.result == 'success'" in job["if"]
    assert "github.ref == 'refs/heads/release'" in job["if"]
    assert job["permissions"] == {"contents": "write"}
    script = "\n".join(step.get("run") or "" for step in job["steps"])
    assert "needs.build.outputs.version" in yaml.dump(job)
    assert "github.sha" in yaml.dump(job)
    assert "PUBLIC_DEPLOY_KEY" not in yaml.dump(job)
    assert "git push" in script and "refs/tags/" in script
    assert "--force" not in script and "tag -f" not in script
    for name, other in jobs.items():
        if name == "tag-private":
            continue
        assert other.get("permissions", {"contents": "read"}) == {"contents": "read"}
    assert "PUBLIC_DEPLOY_KEY" in yaml.dump(jobs["publish"])


def test_public_release_workflow_skips_private_repositories():
    path = WORKFLOWS / "github-release.yml"
    document = load_workflow(path)
    assert document["permissions"] == {"contents": "write"}
    trigger = document["on"]
    assert "main" in trigger["push"]["branches"]
    assert "v*" in trigger["push"]["tags"]
    assert "workflow_dispatch" in trigger
    text = path.read_text(encoding="utf-8")
    assert "scripts/release-notes.py" in text
    assert "Kotoyose" in text
    assert "PUBLIC_DEPLOY_KEY" not in text
    assert "--verify-tag" in text
    assert document["jobs"]
    for job in document["jobs"].values():
        assert "github.event.repository.private == false" in job["if"]


def test_public_release_step_passes_the_built_in_token_to_gh():
    # Actions does not export GITHUB_TOKEN into run steps; gh needs GH_TOKEN set explicitly.
    document = yaml.safe_load((WORKFLOWS / "github-release.yml").read_text(encoding="utf-8"))
    steps = [step for job in document["jobs"].values() for step in job["steps"] if "run" in step]
    assert steps and all(step.get("env", {}).get("GH_TOKEN") == "${{ github.token }}" for step in steps
                         if "gh " in step["run"])
