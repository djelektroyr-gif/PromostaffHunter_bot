import os

import profile_photos as pp


def test_resolve_user_photos_dir_explicit(monkeypatch, tmp_path):
    custom = tmp_path / "custom_photos"
    monkeypatch.setenv("USER_PHOTOS_DIR", str(custom))
    assert pp.resolve_user_photos_dir() == str(custom)


def test_resolve_user_photos_dir_prefers_shared(monkeypatch, tmp_path):
    monkeypatch.delenv("USER_PHOTOS_DIR", raising=False)
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(pp, "get_shared_dir", lambda: str(shared))
    assert pp.resolve_user_photos_dir() == str(shared / "user_photos")


def test_resolve_user_photos_dir_uses_data_volume(monkeypatch, tmp_path):
    monkeypatch.delenv("USER_PHOTOS_DIR", raising=False)
    monkeypatch.setattr(pp, "get_shared_dir", lambda: None)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(pp, "_bothost_data_dir", lambda: str(data))
    assert pp.resolve_user_photos_dir() == str(data / "user_photos")


def test_migrate_legacy_user_photos(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_PHOTOS_DIR", str(tmp_path / "target"))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "123.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(pp, "_legacy_user_photo_dirs", lambda: [str(legacy)])
    moved = pp.migrate_legacy_user_photos()
    assert moved == 1
    assert (tmp_path / "target" / "123.jpg").is_file()


def test_reconcile_photo_storage_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_PHOTOS_DIR", str(tmp_path / "photos"))
    target = tmp_path / "photos" / "42.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    updated = []

    def fake_update(user_id, file_id, storage_path):
        updated.append((user_id, storage_path))

    monkeypatch.setattr(
        "profile_photos.get_subscribers_with_photos",
        lambda: [{"user_id": 42, "photo_file_id": "fid", "photo_storage_path": "/old/path.jpg"}],
        raising=False,
    )
    # patch via db import inside function
    import db

    monkeypatch.setattr(db, "get_subscribers_with_photos", lambda: [
        {"user_id": 42, "photo_file_id": "fid", "photo_storage_path": "/old/path.jpg"},
    ])
    monkeypatch.setattr(db, "update_subscriber_photo_storage", fake_update)
    fixed = pp.reconcile_photo_storage_paths()
    assert fixed == 1
    assert updated[0][0] == 42
    assert updated[0][1] == str(target)
