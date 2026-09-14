import json
from pathlib import Path

from command_center import storage


def test_atomic_write_and_read_json_roundtrip(tmp_path):
    path = tmp_path / "sub" / "file.json"
    storage.atomic_write_json(path, {"a": 1})
    assert storage.read_json(path, None) == {"a": 1}


def test_read_json_missing_file_returns_default(tmp_path):
    assert storage.read_json(tmp_path / "missing.json", "default") == "default"


def test_read_json_corrupt_file_returns_default(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert storage.read_json(path, []) == []


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    storage.atomic_write_json(tmp_path / "file.json", [1, 2, 3])
    assert list(tmp_path.glob(".*tmp")) == []


def test_ensure_seeded_never_reads_sibling_example_file(tmp_path):
    """Regression test: seeding must never copy an illustrative `.example.*` file's
    fake content into the real runtime file — that would present fabricated data as
    real (caught during manual smoke testing before this suite existed)."""
    path = tmp_path / "runtime.json"
    example = tmp_path / "runtime.example.json"
    example.write_text(json.dumps({"should": "not be copied"}), encoding="utf-8")

    storage.ensure_seeded(path, [])

    assert storage.read_json(path, None) == []


def test_ensure_seeded_is_a_noop_if_file_already_exists(tmp_path):
    path = tmp_path / "runtime.json"
    storage.atomic_write_json(path, {"real": "data"})
    storage.ensure_seeded(path, {"default": "value"})
    assert storage.read_json(path, None) == {"real": "data"}


def test_append_jsonl_and_read_jsonl_preserve_order(tmp_path):
    path = tmp_path / "log.jsonl"
    storage.append_jsonl(path, {"id": "a", "v": 1})
    storage.append_jsonl(path, {"id": "a", "v": 2})
    storage.append_jsonl(path, {"id": "b", "v": 1})
    records = storage.read_jsonl(path)
    assert [record["id"] for record in records] == ["a", "a", "b"]


def test_read_jsonl_skips_corrupt_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"id": "a"}\nnot json\n{"id": "b"}\n', encoding="utf-8")
    records = storage.read_jsonl(path)
    assert [record["id"] for record in records] == ["a", "b"]


def test_read_jsonl_missing_file_returns_empty_list(tmp_path):
    assert storage.read_jsonl(tmp_path / "missing.jsonl") == []


def test_ensure_seeded_jsonl_creates_empty_file(tmp_path):
    path = tmp_path / "log.jsonl"
    storage.ensure_seeded_jsonl(path)
    assert path.exists()
    assert storage.read_jsonl(path) == []


def test_fold_latest_by_id_last_write_wins():
    records = [
        {"id": "a", "status": "queued"},
        {"id": "a", "status": "running"},
        {"id": "b", "status": "queued"},
        {"id": "a", "status": "completed"},
    ]
    folded = storage.fold_latest_by_id(records)
    assert folded["a"]["status"] == "completed"
    assert folded["b"]["status"] == "queued"


def test_fold_latest_by_id_ignores_records_without_id():
    records = [{"status": "queued"}, {"id": "a", "status": "completed"}]
    folded = storage.fold_latest_by_id(records)
    assert list(folded.keys()) == ["a"]


def test_resolve_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path / "custom"))
    assert storage.resolve_data_dir(tmp_path) == tmp_path / "custom"


def test_resolve_data_dir_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AICC_DATA_DIR", raising=False)
    assert storage.resolve_data_dir(tmp_path) == tmp_path / "data"


def test_create_json_if_absent_creates_and_reports_it(tmp_path):
    path = tmp_path / "sub" / "store.json"
    assert storage.create_json_if_absent(path, []) is True
    assert storage.read_json(path, None) == []


def test_create_json_if_absent_never_replaces_existing_content(tmp_path):
    """The whole reason this exists instead of `atomic_write_json`: seeding an
    empty default must not erase a record a concurrent writer already
    committed. `atomic_write_json` ends in `os.replace` and would."""
    path = tmp_path / "store.json"
    storage.atomic_write_json(path, [{"id": "ALREADY-COMMITTED"}])

    assert storage.create_json_if_absent(path, []) is False
    assert storage.read_json(path, None) == [{"id": "ALREADY-COMMITTED"}]


def test_ensure_seeded_does_not_replace_existing_content(tmp_path):
    path = tmp_path / "store.json"
    storage.atomic_write_json(path, [{"id": "ALREADY-COMMITTED"}])

    storage.ensure_seeded(path, [])

    assert storage.read_json(path, None) == [{"id": "ALREADY-COMMITTED"}]


def test_ensure_seeded_jsonl_does_not_truncate_a_log_created_in_the_race_window(
    tmp_path, monkeypatch
):
    """Seeding an empty log must create it, never truncate it.

    Two callers can both find the log missing; the slower one then seeds it
    after the faster one has already appended a record. The window is made
    deterministic here rather than raced for: the record is written at the
    moment the seed has prepared the directory and is about to create the
    file. A truncating create loses it; an exclusive one leaves it alone.
    """
    path = tmp_path / "log.jsonl"
    record = '{"id": "appended-in-the-window"}\n'
    real_mkdir = Path.mkdir

    def mkdir_then_let_the_other_writer_in(self: Path, *args, **kwargs):
        result = real_mkdir(self, *args, **kwargs)
        if self == path.parent and not path.exists():
            path.write_text(record, encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_then_let_the_other_writer_in)
    storage.ensure_seeded_jsonl(path)
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == record
