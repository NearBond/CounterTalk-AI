"""Every path under DATA_DIR is built from validated IDs (CODING_GUIDELINES §2.6)."""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from countertalk import storage
from countertalk.errors import Invalid
from countertalk.limits import MAX_SEGMENTS_PER_VISIT, STAGED_FILE_MAX_AGE_MS
from countertalk.models import new_id


def test_a_segment_lives_under_its_visit_as_opaque_bytes(tmp_path: Path) -> None:
    visit_id = new_id()

    path = storage.segment_path(visit_id, 7)

    assert path == tmp_path / "visits" / visit_id / "segments" / "000007.bin"


@pytest.mark.parametrize(
    "visit_id",
    [
        "",
        "../../etc",
        "not-a-uuid",
        "3F2A1B4C-0000-4000-8000-000000000000",
        "{3f2a1b4c-0000-4000-8000-000000000000}",
        "3f2a1b4c000040008000000000000000",
        "3f2a1b4c-0000-4000-8000-000000000000/..",
    ],
)
def test_a_visit_id_that_isnt_a_plain_uuid_is_refused(visit_id: str) -> None:
    with pytest.raises(Invalid):
        storage.visit_dir(visit_id)


@pytest.mark.parametrize("seq", [-1, MAX_SEGMENTS_PER_VISIT, 10**12, True])
def test_a_seq_out_of_range_is_refused_before_any_path(seq: int) -> None:
    with pytest.raises(Invalid) as error:
        storage.segment_path(new_id(), seq)

    assert error.value.code == "invalid_seq"


@pytest.mark.parametrize("ext", ["webm", "m4a", "mp4", "aac", "ogg", "wav"])
def test_an_allowed_extension_passes(ext: str) -> None:
    assert storage.check_ext(ext) == ext


@pytest.mark.parametrize("ext", ["", "exe", "WEBM", ".webm", "../webm", "webm/../x", "mp3"])
def test_any_other_extension_is_refused(ext: str) -> None:
    with pytest.raises(Invalid) as error:
        storage.check_ext(ext)

    assert error.value.code == "unsupported_extension"


def test_staging_writes_the_bytes_inside_data_dir(tmp_path: Path) -> None:
    staged = storage.stage(b"segment bytes")

    assert staged.read_bytes() == b"segment bytes"
    # The same filesystem as the final files, so the move into place is atomic.
    assert staged.is_relative_to(tmp_path)
    assert staged.parent == tmp_path / "tmp"


def test_two_stagings_never_share_a_file() -> None:
    assert storage.stage(b"a") != storage.stage(b"a")


def test_moving_into_place_creates_the_folders_and_replaces_whole(tmp_path: Path) -> None:
    final = storage.segment_path(new_id(), 0)
    storage.move_into_place(storage.stage(b"first"), final)
    second = storage.stage(b"second")

    storage.move_into_place(second, final)

    assert final.read_bytes() == b"second"
    assert not second.exists()


def test_a_path_is_stored_relative_to_data_dir() -> None:
    visit_id = new_id()

    relative = storage.relative_to_data_dir(storage.segment_path(visit_id, 3))

    assert relative == f"visits/{visit_id}/segments/000003.bin"


def test_a_path_outside_data_dir_has_no_relative_form(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        storage.relative_to_data_dir(tmp_path.parent / "elsewhere.bin")


def test_discarding_removes_a_staged_file_and_ignores_one_already_gone() -> None:
    staged = storage.stage(b"x")

    storage.discard(staged)
    storage.discard(staged)

    assert not staged.exists()


def test_discard_never_touches_a_file_outside_the_staging_folder(tmp_path: Path) -> None:
    keep = tmp_path / "keep.bin"
    keep.write_bytes(b"keep")

    with pytest.raises(ValueError):
        storage.discard(keep)

    assert keep.read_bytes() == b"keep"


def test_a_write_that_fails_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "fsync", disk_full)

    with pytest.raises(OSError):
        storage.stage(b"segment bytes")

    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't open a folder to fsync it")
def test_moving_into_place_also_flushes_the_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    flushed_folders: list[int] = []
    real_fsync = os.fsync

    def watch(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            flushed_folders.append(fd)
        real_fsync(fd)

    staged = storage.stage(b"segment bytes")
    monkeypatch.setattr(storage.os, "fsync", watch)

    storage.move_into_place(staged, storage.segment_path(new_id(), 0))

    # Without it, a crash after the commit could lose the rename the database already records.
    assert len(flushed_folders) == 1


# --- sweeping staged files (batch 4) -----------------------------------------------------------

OLD_MS = STAGED_FILE_MAX_AGE_MS + 60_000


def file_aged(path: Path, age_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"half a segment")
    then = time.time() - age_ms / 1000
    os.utime(path, (then, then))
    return path


def test_old_staged_files_are_swept_and_new_ones_kept(tmp_path: Path) -> None:
    old = file_aged(tmp_path / "tmp" / "old.part", OLD_MS)
    new = file_aged(tmp_path / "tmp" / "uploading-now.part", 1_000)

    assert storage.sweep_staged(STAGED_FILE_MAX_AGE_MS) == 1

    assert not old.exists()
    assert new.exists()


def test_only_part_files_in_the_staging_folder_are_swept(tmp_path: Path) -> None:
    kept = [
        file_aged(tmp_path / "tmp" / "notes.txt", OLD_MS),
        file_aged(tmp_path / "visits" / new_id() / "segments" / "000000.part", OLD_MS),
    ]
    folder = tmp_path / "tmp" / "looks-staged.part"
    folder.mkdir()
    then = time.time() - OLD_MS / 1000
    os.utime(folder, (then, then))

    assert storage.sweep_staged(STAGED_FILE_MAX_AGE_MS) == 0

    assert all(path.exists() for path in [*kept, folder])


def test_sweeping_with_no_staging_folder_does_nothing(tmp_path: Path) -> None:
    assert storage.sweep_staged(STAGED_FILE_MAX_AGE_MS) == 0


def test_a_file_another_worker_swept_first_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = file_aged(tmp_path / "tmp" / "old.part", OLD_MS)
    already_gone = tmp_path / "tmp" / "already-gone.part"
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([already_gone, old]))

    assert storage.sweep_staged(STAGED_FILE_MAX_AGE_MS) == 1
    assert not old.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_a_symlink_named_like_a_staged_file_is_left_alone(tmp_path: Path) -> None:
    target = file_aged(tmp_path / "visits" / "keep.bin", OLD_MS)
    link = tmp_path / "tmp" / "link.part"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    then = time.time() - OLD_MS / 1000
    os.utime(link, (then, then), follow_symlinks=False)

    assert storage.sweep_staged(STAGED_FILE_MAX_AGE_MS) == 0

    assert link.is_symlink()
    assert target.exists()


def test_a_streamed_file_appears_whole_and_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = storage.source_path(new_id(), "webm")
    steps: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(storage.os, "fsync", lambda fd: steps.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(
        storage.os, "replace", lambda a, b: steps.append("replace") or real_replace(a, b)
    )

    with storage.writing(target) as out:
        out.write(b"first part ")
        assert not target.exists()  # nothing is in place until the block finishes
        out.write(b"second part")

    assert target.read_bytes() == b"first part second part"
    assert steps[:2] == ["fsync", "replace"]  # the file itself is flushed before it's moved
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_streamed_file_that_fails_leaves_the_old_one_and_no_partial(tmp_path: Path) -> None:
    target = storage.source_path(new_id(), "webm")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"the earlier file")

    with pytest.raises(ValueError, match="^too large$"), storage.writing(target) as out:
        out.write(b"half of it")
        raise ValueError("too large")

    assert target.read_bytes() == b"the earlier file"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_cleanup_that_fails_never_hides_the_real_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stuck(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("held open")

    monkeypatch.setattr(storage.Path, "unlink", stuck)

    with pytest.raises(ValueError, match="^too large$"):
        with storage.writing(storage.source_path(new_id(), "webm")) as out:
            out.write(b"half of it")
            raise ValueError("too large")
