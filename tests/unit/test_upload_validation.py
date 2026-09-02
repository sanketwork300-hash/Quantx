"""Upload hardening."""

from __future__ import annotations

import pytest

from domains.market_data.service import UploadRejected, sanitize_filename


class TestFilenameSanitisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("chain.csv", "chain.csv"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\config", "config"),
            ("/absolute/path/chain.csv", "chain.csv"),
            ("weird;name|with$chars.csv", "weirdnamewithchars.csv"),
            ("", "upload"),
        ],
    )
    def test_path_components_and_metacharacters_are_stripped(self, raw, expected):
        assert sanitize_filename(raw) == expected

    def test_length_is_capped(self):
        assert len(sanitize_filename("a" * 500 + ".csv")) <= 255


class TestContentValidation:
    @pytest.fixture
    def service(self, tmp_path):
        from domains.market_data.service import MarketDataService
        from infrastructure.settings import Settings
        from infrastructure.storage.local import LocalObjectStore

        settings = Settings(max_upload_bytes=1024, object_store_root=tmp_path)
        return MarketDataService(None, settings, LocalObjectStore(tmp_path))

    def test_accepts_a_csv(self, service):
        service.validate_upload("chain.csv", "text/csv", b"a,b\n1,2\n")

    def test_rejects_an_empty_file(self, service):
        with pytest.raises(UploadRejected) as exc:
            service.validate_upload("chain.csv", "text/csv", b"")
        assert exc.value.code == "UPLOAD_EMPTY"

    def test_rejects_an_over_sized_file(self, service):
        with pytest.raises(UploadRejected) as exc:
            service.validate_upload("chain.csv", "text/csv", b"x" * 2048)
        assert exc.value.code == "UPLOAD_TOO_LARGE"

    def test_rejects_a_disallowed_extension(self, service):
        with pytest.raises(UploadRejected) as exc:
            service.validate_upload("chain.xlsx", "text/csv", b"a,b\n1,2\n")
        assert exc.value.code == "UPLOAD_EXTENSION_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "signature",
        [b"PK\x03\x04", b"%PDF-1.7", b"\x7fELF", b"MZ\x90\x00", b"\x89PNG\r\n"],
    )
    def test_rejects_binary_content_regardless_of_the_filename(self, service, signature):
        """The extension is attacker-controlled; the content is what gets parsed."""
        with pytest.raises(UploadRejected) as exc:
            service.validate_upload("chain.csv", "text/csv", signature + b"rest")
        assert exc.value.code == "UPLOAD_BINARY_CONTENT"

    def test_rejects_non_utf8_content(self, service):
        with pytest.raises(UploadRejected) as exc:
            service.validate_upload("chain.csv", "text/csv", b"\xff\xfe\x00bad")
        assert exc.value.code == "UPLOAD_NOT_UTF8"
