import io
import zipfile
from datetime import date
from pathlib import Path

from extractor import analyze
from loader import collect_files

DOCS = Path(__file__).parent.parent / "documents"


def sample():
    return [(p.name, p.read_bytes()) for p in sorted(DOCS.glob("*.txt"))]


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_with_subfolders_gives_same_result_as_files():
    z = make_zip([(f"комплект/{n}", d) for n, d in sample()] + [("комплект/readme.pdf", b"%PDF")])
    files, notes = collect_files([("docs.zip", z)])
    assert sorted(files) == sorted(n for n, _ in sample())
    assert any("не в формате TXT — 1" in n for n in notes)
    a = analyze(files, date(2026, 3, 10))
    assert a.get("STN-101").delivery.value == date(2026, 4, 30)
    assert a.get("STN-103").amount.value is None


def test_folder_upload_paths_and_junk_skipped():
    uploads = [(f"documents/{n}", d) for n, d in sample()]
    uploads += [("documents/.DS_Store", b"x"), ("documents/__MACOSX/._01_contract.txt", b"x")]
    files, notes = collect_files(uploads)
    assert len(files) == 6
    assert "01_contract.txt" in files


def test_mixed_txt_and_zip_dedup_identical_content():
    s = sample()
    z = make_zip(s[:3])
    files, notes = collect_files(s + [("part.zip", z)])
    assert len(files) == 6
    assert sum("учтён один раз" in n for n in notes) == 3
    assert not analyze(files, date(2026, 3, 10)).get("STN-101").flags


def test_same_name_different_content_kept_both():
    files, _ = collect_files([("a/contract.txt", b"one"), ("b/contract.txt", b"two")])
    assert len(files) == 2 and set(files.values()) == {"one", "two"}


def test_bad_zip_and_unsupported_type_reported():
    files, notes = collect_files([("broken.zip", b"not a zip"), ("scan.pdf", b"%PDF")])
    assert files == {}
    assert any("не удалось открыть" in n for n in notes)
    assert any("только TXT и ZIP" in n for n in notes)


def test_zip_without_txt():
    files, notes = collect_files([("empty.zip", make_zip([("a.docx", b"x")]))])
    assert files == {} and any("нет TXT" in n for n in notes)


def test_cp866_names_from_windows_archivers():
    class Cp866Info(zipfile.ZipInfo):
        # имя пишется байтами CP866 без флага UTF-8 — как у архиваторов Windows
        def _encodeFilenameFlags(self):
            return self.filename.encode("cp866"), self.flag_bits & ~0x800

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(Cp866Info("договор.txt"), "ДОГОВОР ПОСТАВКИ № X-1".encode("utf-8"))
    files, _ = collect_files([("w.zip", buf.getvalue())])
    assert list(files) == ["договор.txt"]
