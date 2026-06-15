from openhachimi_agent.storage.attachments import AttachmentError, AttachmentStorage


def make_storage(tmp_path):
    return AttachmentStorage(
        tmp_path / ".tmp" / "attachments",
        max_size_bytes=1024,
        allowed_mime_types=[],
        workspace_root=tmp_path,
    )


def test_sanitize_filename_removes_paths_and_reserved_names(tmp_path):
    storage = make_storage(tmp_path)

    assert storage.sanitize_filename("CON", "text/plain") == "CON_file.txt"
    assert storage.sanitize_filename("bad:name?.txt", "text/plain") == "bad_name.txt"


def test_validate_rejects_path_filename(tmp_path):
    storage = make_storage(tmp_path)

    try:
        storage.validate_metadata(filename="../secret.txt", content_type="text/plain", size_bytes=1)
    except AttachmentError as exc:
        assert "路径" in str(exc)
    else:
        raise AssertionError("path filename should be rejected")


def test_validate_rejects_large_files_but_accepts_any_mime(tmp_path):
    storage = make_storage(tmp_path)

    try:
        storage.validate_metadata(filename="a.txt", content_type="text/plain", size_bytes=2048)
    except AttachmentError:
        pass
    else:
        raise AssertionError("oversized attachment should be rejected")

    storage.validate_metadata(filename="a.exe", content_type="application/x-msdownload", size_bytes=1)


def test_build_path_stays_under_base_dir(tmp_path):
    storage = make_storage(tmp_path)

    path = storage.build_path(source="telegram", namespace="u1/m1", filename="a.txt", content_type="text/plain")

    assert path.name == "a.txt"
    path.resolve().relative_to(storage.base_dir.resolve())


def test_to_ref_records_safe_metadata(tmp_path):
    storage = make_storage(tmp_path)
    path = storage.build_path(source="telegram", namespace="u1", filename="a.txt", content_type="text/plain")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello", encoding="utf-8")

    ref = storage.to_ref(path=path, source="telegram", filename="a.txt", content_type="text/plain", size_bytes=None)

    assert ref.source == "telegram"
    assert ref.kind == "document"
    assert ref.filename == "a.txt"
    assert ref.size_bytes == 5
    assert ref.local_path == ".tmp/attachments/telegram/u1/a.txt"


def test_to_ref_preserves_weixin_source_and_video_kind(tmp_path):
    storage = make_storage(tmp_path)
    path = storage.build_path(source="weixin", namespace="u1", filename="clip.mp4", content_type="video/mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")

    ref = storage.to_ref(path=path, source="weixin", filename="clip.mp4", content_type="video/mp4", size_bytes=None)

    assert ref.source == "weixin"
    assert ref.kind == "video"
