from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.tools.artifacts import publish_artifact


def make_ctx(tmp_path, max_size=1024):
    config = SimpleNamespace(base_dir=tmp_path, attachments_dir=tmp_path / ".tmp" / "attachments", max_attachment_size_bytes=max_size)
    deps = SimpleNamespace(base_dir=tmp_path, skills_dirs=[], config=config, session_state={})
    return SimpleNamespace(deps=deps)


def test_publish_artifact_records_turn_artifact(tmp_path):
    target = tmp_path / "report.md"
    target.write_text("hello", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    result = publish_artifact(ctx, "report.md", title="报告")

    artifact = result["artifact"]
    assert artifact["filename"] == "report.md"
    assert artifact["content_type"] == "text/markdown"
    assert artifact["download_url"].startswith("/artifacts/")
    assert artifact["local_path"].startswith(".tmp/artifacts/art_")
    assert artifact["local_path"].endswith("/report.md")
    assert (tmp_path / artifact["local_path"]).read_text(encoding="utf-8") == "hello"
    assert len(ctx.deps.session_state["turn_artifacts"]) == 1
    assert ctx.deps.session_state["turn_artifacts"][0].title == "报告"


def test_publish_artifact_sanitizes_filename_override(tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("hello", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    result = publish_artifact(ctx, "data.txt", filename="bad:name?.csv")

    assert result["artifact"]["filename"] == "bad_name_.csv"
    assert result["artifact"]["content_type"] in {"text/csv", "application/vnd.ms-excel"}

def test_publish_artifact_resolves_relative_path_from_cwd(tmp_path):
    image = tmp_path / "tmp" / "doubao-seedream" / "cute_kitten.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpg")
    ctx = make_ctx(tmp_path)

    result = publish_artifact(ctx, "cute_kitten.jpg", cwd="tmp/doubao-seedream")

    assert result["artifact"]["local_path"].startswith(".tmp/artifacts/art_")
    assert result["artifact"]["local_path"].endswith("/cute_kitten.jpg")
    assert (tmp_path / result["artifact"]["local_path"]).read_bytes() == b"jpg"
    image.unlink()
    assert (tmp_path / result["artifact"]["local_path"]).read_bytes() == b"jpg"


@pytest.mark.parametrize("path", ["missing.txt", ".", "../outside.txt"])
def test_publish_artifact_rejects_invalid_paths(tmp_path, path):
    (tmp_path.parent / "outside.txt").write_text("outside", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    with pytest.raises(ModelRetry):
        publish_artifact(ctx, path)


def test_publish_artifact_rejects_sensitive_and_large_files(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=x", encoding="utf-8")
    big_file = tmp_path / "big.bin"
    big_file.write_bytes(b"x" * 20)

    with pytest.raises(ModelRetry):
        publish_artifact(make_ctx(tmp_path), ".env")
    with pytest.raises(ModelRetry):
        publish_artifact(make_ctx(tmp_path, max_size=10), "big.bin")
