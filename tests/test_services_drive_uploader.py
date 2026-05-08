from pathlib import Path

import services.drive_uploader as du


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Files:
    def __init__(self):
        self.created = []
        self.to_list = {"files": []}

    def list(self, **kwargs):
        return _Exec(self.to_list)

    def create(self, body=None, media_body=None, fields=None):
        self.created.append((body, media_body, fields))
        return _Exec({"id": "file_or_folder_id"})


class _Perms:
    def create(self, **kwargs):
        return _Exec({})


class _Service:
    def __init__(self):
        self._files = _Files()
        self._perms = _Perms()

    def files(self):
        return self._files

    def permissions(self):
        return self._perms


def test_timestamped_filename_prefix():
    name = du._timestamped_filename()
    assert name.startswith("NG360_Quote_")
    assert name.endswith(".pdf")


def test_get_shareable_url_format():
    assert du._get_shareable_url("abc").endswith("/abc/view?usp=sharing")


def test_get_or_create_folder_returns_existing(monkeypatch):
    monkeypatch.setattr(du, "_find_folder", lambda *args, **kwargs: "existing")
    monkeypatch.setattr(du, "_create_folder", lambda *args, **kwargs: "new")
    assert du._get_or_create_folder(object(), "name") == "existing"


def test_upload_quote_pdf_missing_file_raises(tmp_path):
    missing = tmp_path / "missing.pdf"
    try:
        du.upload_quote_pdf(missing, "A", "B")
    except FileNotFoundError:
        pass
    else:
        assert False, "Expected FileNotFoundError"


def test_upload_quote_pdf_happy_path(tmp_path, monkeypatch):
    pdf = tmp_path / "q.pdf"
    pdf.write_text("fake")

    service = _Service()

    monkeypatch.setattr(du, "GDRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(du, "_build_service", lambda: service)
    monkeypatch.setattr(du, "_get_or_create_folder", lambda *args, **kwargs: "customer_folder")
    monkeypatch.setattr(du, "_make_shareable", lambda *args, **kwargs: None)
    monkeypatch.setattr(du, "_timestamped_filename", lambda: "NG360_Quote_20260101_010101.pdf")
    monkeypatch.setattr(du, "MediaFileUpload", lambda *args, **kwargs: object())

    url = du.upload_quote_pdf(pdf, "John", "Doe")
    assert "drive.google.com" in url


def test_upload_quote_pdf_requires_root_folder_id(tmp_path, monkeypatch):
    pdf = tmp_path / "q.pdf"
    pdf.write_text("fake")
    monkeypatch.setattr(du, "GDRIVE_FOLDER_ID", "")
    monkeypatch.setattr(du, "_build_service", lambda: _Service())

    try:
        du.upload_quote_pdf(pdf, "A", "B")
    except du.DriveError as exc:
        assert "GDRIVE_FOLDER_ID" in str(exc)
    else:
        assert False, "Expected DriveError"
