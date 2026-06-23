# pyrefly: ignore [missing-import]
import hashlib
import io
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import openhachimi_agent.tools.skills as skills_tools
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


def test_find_skills_dedupes_same_name_across_dirs_first_wins(tmp_path):
    """两个 skills_dirs 里出现同名 skill 时,只保留第一个目录的版本(first wins)。

    场景:用户把 ``user/skills`` 和 ``external_skills_dir`` 都装了同名 skill。
    旧版会把两条都返回,导致 system prompt 里技能索引出现重复行;新版按
    ``config.name`` 去重,保留前者。
    """
    primary_dir = tmp_path / "primary"
    external_dir = tmp_path / "external"

    primary_skill = _write_skill(
        primary_dir / "demo-skill",
        "name: demo-skill\ndescription: Primary copy",
        "primary body",
    )
    _write_skill(
        external_dir / "demo-skill",
        "name: demo-skill\ndescription: External copy",
        "external body",
    )

    try:
        result = find_skills([primary_dir, external_dir])
        assert [s.config.name for s in result] == ["demo-skill"]
        # 保留的是 primary_dir 的版本(first wins),不是外部目录的
        assert result[0].path == primary_skill.path
        assert result[0].config.description == "Primary copy"
        assert "primary body" in result[0].body
    finally:
        with _SKILLS_CACHE_LOCK:
            _SKILLS_CACHE.clear()


def test_find_skills_keeps_different_names_across_dirs(tmp_path):
    """两个目录都有 skill 但名字不同时,两个都应保留。"""
    primary_dir = tmp_path / "primary"
    external_dir = tmp_path / "external"

    _write_skill(
        primary_dir / "skill-a",
        "name: skill-a\ndescription: A",
        "a",
    )
    _write_skill(
        external_dir / "skill-b",
        "name: skill-b\ndescription: B",
        "b",
    )

    try:
        result = find_skills([primary_dir, external_dir])
        names = sorted(s.config.name for s in result)
        assert names == ["skill-a", "skill-b"]
    finally:
        with _SKILLS_CACHE_LOCK:
            _SKILLS_CACHE.clear()


def test_find_skills_dedupes_within_single_dir(tmp_path):
    """同一目录下两个不同子文件夹用了同一个 frontmatter name —— 也应去重。

    实务场景:用户从 zip 解压 skill 时不小心解出两份,或者手动复制后忘了改 name。
    保留 ``os.walk`` 先返回的那条(平台行为通常是字母序)。
    """
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir / "aaa-copy",
        "name: shared-skill\ndescription: First copy",
        "first body",
    )
    _write_skill(
        skills_dir / "zzz-copy",
        "name: shared-skill\ndescription: Second copy",
        "second body",
    )

    try:
        result = find_skills([skills_dir])
        assert len(result) == 1
        assert result[0].config.name == "shared-skill"
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


def test_build_skill_tool_always_returns_full_wrapper(tmp_path):
    """渐进披露后,被动注入路径已废除。无论 session_state 状态如何,带 arguments
    的宏工具被调用时,总是返回参数化 body + 完整 wrapper(name / path / 执行 intro)。

    这条用例兜底:防止有人误以为还要靠 ``injected_skill_names`` 做去重而再加回那段
    分支逻辑。"""
    skill = _write_skill(
        tmp_path / "skills" / "argument-skill",
        "name: argument-skill\ndescription: Argument skill\narguments:\n  - target",
        "请读取 references/{{target}}.md",
    )
    tool_func = build_skill_tool(skill)
    args_model = tool_func.__annotations__["args"]

    # 即便 session_state 假装"已经注入过同名 skill",新行为下也应返回完整 wrapper
    deps = SimpleNamespace(session_state={"injected_skill_names": ["argument-skill"]})
    ctx = SimpleNamespace(deps=deps)

    result = tool_func(ctx, args_model(target="guide"))

    # 参数已被填充
    assert "references/guide.md" in result
    assert "{{target}}" not in result
    # wrapper 完整存在
    assert "【Skill Execution:" in result
    assert "[Skill Metadata]" in result
    assert "skill_root" in result


def test_download_url_uses_explicit_timeout(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse(io.BytesIO):
        headers = {}

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

    digest = _download_url("https://example.com/skill.zip", download_path)

    assert captured == {
        "source_url": "https://example.com/skill.zip",
        "timeout": _SKILL_DOWNLOAD_TIMEOUT_SECONDS,
    }
    assert download_path.read_bytes() == b"archive-bytes"
    assert digest == hashlib.sha256(b"archive-bytes").hexdigest()


def test_download_url_rejects_content_length_over_limit(tmp_path, monkeypatch):
    class FakeResponse(io.BytesIO):
        headers = {"Content-Length": "11"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda source_url, timeout: FakeResponse(b"small"))

    with pytest.raises(ValueError, match="too large"):
        _download_url("https://example.com/skill.zip", tmp_path / "archive", max_bytes=10)


def test_download_url_rejects_stream_over_limit(tmp_path, monkeypatch):
    class FakeResponse(io.BytesIO):
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda source_url, timeout: FakeResponse(b"too-large"))

    with pytest.raises(ValueError, match="exceeded maximum size"):
        _download_url("https://example.com/skill.zip", tmp_path / "archive", max_bytes=3)


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


@pytest.mark.parametrize("skill_name", ["../evil", "..\\evil", "/tmp/evil", "C:\\evil", "CON", "con.txt", "bad/name"])
def test_install_skill_rejects_unsafe_skill_name(tmp_path, skill_name):
    source_skill = tmp_path / "source" / "malicious"
    _write_skill(source_skill, f"name: {skill_name}\ndescription: Malicious", "恶意内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, str(source_skill))

    assert "Invalid skill name" in result
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path / "user" / "evil").exists()


def test_install_skill_allows_safe_dotted_skill_name(tmp_path):
    source_skill = tmp_path / "source" / "demo"
    _write_skill(source_skill, "name: demo.skill_1\ndescription: Demo", "内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, str(source_skill))

    assert "successfully installed" in result
    assert (tmp_path / "user" / "skills" / "demo.skill_1" / "SKILL.md").exists()


def test_install_skill_rejects_http_by_default(tmp_path, monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("urlopen should not be called for rejected HTTP sources")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, "http://example.com/skill.zip")

    assert "HTTP skill sources are disabled" in result


def test_install_skill_rejects_unsupported_url_scheme(tmp_path):
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, "ftp://example.com/skill.zip")

    assert "Unsupported URL scheme" in result


def test_install_skill_rejects_invalid_sha256(tmp_path):
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, "https://example.com/skill.zip", expected_sha256="bad")

    assert "expected_sha256" in result


def test_install_skill_rejects_sha256_for_local_source(tmp_path):
    source_skill = tmp_path / "source" / "demo"
    _write_skill(source_skill, "name: demo\ndescription: Demo", "内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, str(source_skill), expected_sha256="0" * 64)

    assert "expected_sha256 is only supported" in result


def test_install_skill_download_archive_checks_sha256(tmp_path, monkeypatch):
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as zip_ref:
        zip_ref.writestr("demo/SKILL.md", "---\nname: demo\ndescription: Demo\n---\n\n内容")
    payload = archive_bytes.getvalue()
    digest = hashlib.sha256(payload).hexdigest()

    class FakeResponse(io.BytesIO):
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda source_url, timeout: FakeResponse(payload))
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    mismatch = install_skill(ctx, "https://example.com/skill.zip", expected_sha256="0" * 64)
    result = install_skill(ctx, "https://example.com/skill.zip", expected_sha256=digest)

    assert "SHA-256 mismatch" in mismatch
    assert "successfully installed" in result
    assert (tmp_path / "user" / "skills" / "demo" / "SKILL.md").exists()


def test_install_skill_update_preserves_existing_skill_when_staging_copy_fails(tmp_path, monkeypatch):
    source_skill = tmp_path / "source" / "demo-install"
    skill_file = source_skill / "SKILL.md"
    _write_skill(source_skill, "name: demo-install\ndescription: Demo install", "初始内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))
    install_skill(ctx, str(source_skill))
    skill_file.write_text("---\nname: demo-install\ndescription: Demo install\n---\n\n更新内容", encoding="utf-8")

    def fail_copytree(src, dest):
        raise OSError("copy failed")

    monkeypatch.setattr(skills_tools, "_copytree", fail_copytree)

    result = install_skill(ctx, str(source_skill))

    dest_skill_file = tmp_path / "user" / "skills" / "demo-install" / "SKILL.md"
    assert "copy failed" in result
    assert "初始内容" in dest_skill_file.read_text(encoding="utf-8")


def test_install_skill_update_rolls_back_when_staging_rename_fails(tmp_path, monkeypatch):
    source_skill = tmp_path / "source" / "demo-install"
    skill_file = source_skill / "SKILL.md"
    _write_skill(source_skill, "name: demo-install\ndescription: Demo install", "初始内容")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))
    install_skill(ctx, str(source_skill))
    skill_file.write_text("---\nname: demo-install\ndescription: Demo install\n---\n\n更新内容", encoding="utf-8")

    original_rename = Path.rename

    def fail_staging_rename(self, target):
        if ".staging." in self.name:
            raise OSError("rename failed")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_staging_rename)

    result = install_skill(ctx, str(source_skill))

    dest_skill_file = tmp_path / "user" / "skills" / "demo-install" / "SKILL.md"
    assert "Existing installation was restored" in result
    assert "初始内容" in dest_skill_file.read_text(encoding="utf-8")
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


def test_safe_extract_zip_rejects_too_many_entries(tmp_path):
    archive_path = tmp_path / "many.zip"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("a.txt", "a")
        zip_ref.writestr("b.txt", "b")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        with pytest.raises(ValueError, match="too many entries"):
            _safe_extract_zip(zip_ref, tmp_path / "repo", max_members=1)


def test_safe_extract_zip_rejects_uncompressed_size_limit(tmp_path):
    archive_path = tmp_path / "large.zip"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("a.txt", "12345")
        zip_ref.writestr("b.txt", "67890")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        with pytest.raises(ValueError, match="uncompressed size exceeds"):
            _safe_extract_zip(zip_ref, tmp_path / "repo", max_total_uncompressed_bytes=6)


def test_safe_extract_zip_rejects_file_size_limit(tmp_path):
    archive_path = tmp_path / "large-file.zip"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("a.txt", "12345")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        with pytest.raises(ValueError, match="too large"):
            _safe_extract_zip(zip_ref, tmp_path / "repo", max_file_bytes=3)


def test_safe_extract_zip_rejects_symlink_entries(tmp_path):
    archive_path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr(info, "target")

    with zipfile.ZipFile(archive_path, "r") as zip_ref:
        with pytest.raises(ValueError, match="Unsafe zip member type"):
            _safe_extract_zip(zip_ref, tmp_path / "repo")


def test_safe_extract_tar_rejects_too_many_entries(tmp_path):
    archive_path = tmp_path / "many.tar"
    with tarfile.open(archive_path, "w") as tar_ref:
        for name in ["a.txt", "b.txt"]:
            data = b"x"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar_ref.addfile(member, io.BytesIO(data))

    with tarfile.open(archive_path, "r") as tar_ref:
        with pytest.raises(ValueError, match="too many entries"):
            _safe_extract_tar(tar_ref, tmp_path / "repo", max_members=1)


def test_safe_extract_tar_rejects_uncompressed_size_limit(tmp_path):
    archive_path = tmp_path / "large.tar"
    with tarfile.open(archive_path, "w") as tar_ref:
        for name in ["a.txt", "b.txt"]:
            data = b"12345"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar_ref.addfile(member, io.BytesIO(data))

    with tarfile.open(archive_path, "r") as tar_ref:
        with pytest.raises(ValueError, match="uncompressed size exceeds"):
            _safe_extract_tar(tar_ref, tmp_path / "repo", max_total_uncompressed_bytes=6)


def test_safe_extract_tar_rejects_file_size_limit(tmp_path):
    archive_path = tmp_path / "large-file.tar"
    data = b"12345"
    with tarfile.open(archive_path, "w") as tar_ref:
        member = tarfile.TarInfo("a.txt")
        member.size = len(data)
        tar_ref.addfile(member, io.BytesIO(data))

    with tarfile.open(archive_path, "r") as tar_ref:
        with pytest.raises(ValueError, match="too large"):
            _safe_extract_tar(tar_ref, tmp_path / "repo", max_file_bytes=3)


def test_install_skill_rejects_local_symlink(tmp_path):
    source_skill = tmp_path / "source" / "demo"
    _write_skill(source_skill, "name: demo\ndescription: Demo", "内容")
    link_path = source_skill / "link"
    try:
        link_path.symlink_to(source_skill / "SKILL.md")
    except OSError:
        pytest.skip("symlink creation is not available")
    ctx = SimpleNamespace(deps=SimpleNamespace(base_dir=tmp_path, skills_dirs=[tmp_path / "user" / "skills"]))

    result = install_skill(ctx, str(source_skill))

    assert "unsupported symlink" in result
