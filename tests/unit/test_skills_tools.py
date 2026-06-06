# pyrefly: ignore [missing-import]
import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from openhachimi_agent.content.skills import _SKILLS_CACHE, _SKILLS_CACHE_LOCK, find_skills, parse_skill
from openhachimi_agent.tools.skills import (
    _SKILL_DOWNLOAD_TIMEOUT_SECONDS,
    _SKILL_GIT_CLONE_TIMEOUT_SECONDS,
    _download_url,
    _safe_extract_tar,
    _safe_extract_zip,
    build_skill_tool,
    format_skill_prompt,
    get_skill_instructions,
    install_skill,
)


def _write_skill(skill_dir, frontmatter: str, body: str):
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    skill = parse_skill(skill_file)
    assert skill is not None
    return skill


def _make_ctx(skills_dir):
    return SimpleNamespace(deps=SimpleNamespace(skills_dirs=[skills_dir]))


def test_find_skills_returns_copy_of_cached_list(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir / "demo-skill", "name: demo-skill\ndescription: Demo skill", "内容")

    try:
        first_result = find_skills([skills_dir])
        first_result.clear()
        second_result = find_skills([skills_dir])

        assert [skill.config.name for skill in second_result] == ["demo-skill"]
    finally:
        with _SKILLS_CACHE_LOCK:
            _SKILLS_CACHE.clear()


def test_get_skill_instructions_includes_skill_path_metadata(tmp_path):
    skills_dir = tmp_path / "external_skills"
    skill = _write_skill(
        skills_dir / "demo-skill",
        "name: demo-skill\ndescription: Demo skill",
        "请读取 references/guide.md",
    )

    result = get_skill_instructions(_make_ctx(skills_dir), "demo-skill")

    assert f"skill_root: {skill.path.parent.resolve().as_posix()}" in result
    assert f"skill_path: {skill.path.resolve().as_posix()}" in result
    assert "相对路径仍相对于当前项目工作区根目录" in result
    assert "拼接成绝对路径" in result
    assert "请读取 references/guide.md" in result


def test_get_skill_instructions_keeps_disable_model_invocation_behavior(tmp_path):
    skills_dir = tmp_path / "external_skills"
    _write_skill(
        skills_dir / "disabled-skill",
        "name: disabled-skill\ndescription: Disabled skill\ndisable-model-invocation: true",
        "不应该返回这段正文",
    )

    result = get_skill_instructions(_make_ctx(skills_dir), "disabled-skill")

    assert "disable_model_invocation=true" in result
    assert "不应该返回这段正文" not in result
    assert "skill_root" not in result


def test_format_skill_prompt_wraps_body_with_path_note(tmp_path):
    skill = _write_skill(
        tmp_path / "skills" / "demo-skill",
        "name: demo-skill\ndescription: Demo skill",
        "读取 templates/example.md",
    )

    result = format_skill_prompt(skill)

    assert result.startswith("<skill name=\"demo-skill\"")
    assert f"skill_root=\"{skill.path.parent.resolve().as_posix()}\"" in result
    assert f"path=\"{skill.path.resolve().as_posix()}\"" in result
    assert "读取 templates/example.md" in result
    assert result.endswith("</skill>")


def test_build_skill_tool_replaces_arguments_and_includes_skill_root(tmp_path):
    skill = _write_skill(
        tmp_path / "skills" / "argument-skill",
        "name: argument-skill\ndescription: Argument skill\narguments:\n  - target",
        "请读取 references/{{target}}.md",
    )
    tool_func = build_skill_tool(skill)
    args_model = tool_func.__annotations__["args"]

    result = tool_func(SimpleNamespace(), args_model(target="guide"))

    assert "【Skill Execution: argument-skill】" in result
    assert "references/guide.md" in result
    assert "{{target}}" not in result
    assert f"skill_root: {skill.path.parent.resolve().as_posix()}" in result
    assert "拼接成绝对路径" in result


def test_download_url_uses_explicit_timeout(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(source_url, timeout):
        captured["source_url"] = source_url
        captured["timeout"] = timeout
        return FakeResponse(b"archive-bytes")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    download_path = tmp_path / "downloaded_archive"

    _download_url("https://example.com/skill.zip", download_path)

    assert captured == {
        "source_url": "https://example.com/skill.zip",
        "timeout": _SKILL_DOWNLOAD_TIMEOUT_SECONDS,
    }
    assert download_path.read_bytes() == b"archive-bytes"


def test_install_skill_git_clone_uses_explicit_timeout(tmp_path, monkeypatch):
    captured = {}
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        repo_dir = Path(args[0][-1])
        _write_skill(repo_dir / "demo-git", "name: demo-git\ndescription: Demo git", "Git 内容")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = install_skill(ctx, "https://example.com/demo.git")

    assert "successfully installed" in result
    assert captured["kwargs"]["timeout"] == _SKILL_GIT_CLONE_TIMEOUT_SECONDS
    assert (tmp_path / "user" / "skills" / "demo-git" / "SKILL.md").exists()


def test_install_skill_reports_git_clone_timeout(tmp_path, monkeypatch):
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)

    result = install_skill(ctx, "https://example.com/demo.git")

    assert "operation timed out" in result
    assert str(_SKILL_GIT_CLONE_TIMEOUT_SECONDS) in result


def test_install_skill_installs_local_skill_into_project_user_skills(tmp_path):
    source_skill = tmp_path / "source" / "demo-install"
    _write_skill(source_skill, "name: demo-install\ndescription: Demo install", "初始内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, str(source_skill))

    dest_skill_file = tmp_path / "user" / "skills" / "demo-install" / "SKILL.md"
    assert "successfully installed" in result
    assert dest_skill_file.exists()
    assert "初始内容" in dest_skill_file.read_text(encoding="utf-8")


def test_install_skill_reports_up_to_date_for_same_local_skill(tmp_path):
    source_skill = tmp_path / "source" / "demo-install"
    _write_skill(source_skill, "name: demo-install\ndescription: Demo install", "初始内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    install_skill(ctx, str(source_skill))
    result = install_skill(ctx, str(source_skill))

    assert "already installed and is up-to-date" in result


def test_install_skill_updates_existing_local_skill(tmp_path):
    source_skill = tmp_path / "source" / "demo-install"
    skill_file = source_skill / "SKILL.md"
    _write_skill(source_skill, "name: demo-install\ndescription: Demo install", "初始内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    install_skill(ctx, str(source_skill))
    skill_file.write_text("---\nname: demo-install\ndescription: Demo install\n---\n\n更新内容", encoding="utf-8")
    result = install_skill(ctx, str(source_skill))

    dest_skill_file = tmp_path / "user" / "skills" / "demo-install" / "SKILL.md"
    assert "successfully updated" in result
    assert "更新内容" in dest_skill_file.read_text(encoding="utf-8")


def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "malicious.zip"
    outside_path = tmp_path / "outside.txt"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("../outside.txt", "escaped")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        with pytest.raises(ValueError, match="Unsafe archive member path"):
            _safe_extract_zip(zip_ref, tmp_path / "repo")

    assert not outside_path.exists()


def test_safe_extract_tar_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "malicious.tar"
    outside_path = tmp_path / "outside.txt"
    data = b"escaped"
    with tarfile.open(archive_path, "w") as tar_ref:
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(data)
        tar_ref.addfile(member, io.BytesIO(data))

    with tarfile.open(archive_path, "r") as tar_ref:
        with pytest.raises(ValueError, match="Unsafe archive member path"):
            _safe_extract_tar(tar_ref, tmp_path / "repo")

    assert not outside_path.exists()


def test_safe_extract_tar_rejects_symlinks(tmp_path):
    archive_path = tmp_path / "malicious.tar"
    with tarfile.open(archive_path, "w") as tar_ref:
        member = tarfile.TarInfo("repo/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../outside.txt"
        tar_ref.addfile(member)

    with tarfile.open(archive_path, "r") as tar_ref:
        with pytest.raises(ValueError, match="Unsafe tar member type"):
            _safe_extract_tar(tar_ref, tmp_path / "repo")
