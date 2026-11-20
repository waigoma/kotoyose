"""Regression tests for generic verification artifact media."""
import pytest
from kotoyose.artifact_media import validate_artifact_media_type
from kotoyose.execution_artifacts import ArtifactCaptureSpec, ArtifactSpecEntry, collect_execution_artifacts

@pytest.mark.parametrize("value", ["application/xml", "application/vnd.junit+xml", "text/csv", "application/x-lcov", "image/png", "video/webm"])
def test_generic_types(value):
    assert validate_artifact_media_type(value) == value

@pytest.mark.parametrize("value", ["", "text/html; charset=utf-8", "text/plain\r\nX-Test: evil", "text/plain ", "/plain", "text/", "x/" + "a" * 128])
def test_header_and_size_guards(value):
    with pytest.raises(ValueError):
        validate_artifact_media_type(value)

def test_collector_preserves_generic_payload(tmp_path):
    payload = b"<testsuite tests='1' failures='0'/>"
    (tmp_path / "report.xml").write_bytes(payload)
    info = tmp_path.stat()
    entry = ArtifactSpecEntry(name="junit", path="report.xml", media_type="application/xml", required=True, max_bytes=1024)
    spec = ArtifactCaptureSpec(schema_version="1", kind="artifact_spec", entries=[entry], max_total_bytes=1024)
    result = collect_execution_artifacts(tmp_path, info.st_dev, info.st_ino, spec)
    assert result.contract_valid
    assert result.artifacts["junit"].content == payload
    assert result.artifacts["junit"].media_type == "application/xml"

def test_generic_media_does_not_relax_path_contract():
    with pytest.raises(ValueError):
        ArtifactSpecEntry(name="junit", path="../outside.xml", media_type="application/xml", required=True, max_bytes=1024)
