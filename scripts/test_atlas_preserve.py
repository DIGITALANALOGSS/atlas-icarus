import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "atlas_preserve", Path(__file__).with_name("atlas_preserve.py")
)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

RECEIVED = "2026-10-08T12:00:00Z"


class PreservationTests(unittest.TestCase):
    def preserve(self, source, destination):
        return tool.preserve(
            source, destination, custodian="synthetic custodian",
            source_label="synthetic test", received_at=RECEIVED,
        )

    def test_binary_and_empty_files_preserved(self):
        for data in (b"\x00\xff\r\nbinary", b""):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "original.dat"
                destination = root / "storage"
                destination.mkdir()
                source.write_bytes(data)
                result = self.preserve(source, destination)
                self.assertEqual(source.read_bytes(), data)
                self.assertEqual(Path(result["preserved_copy"]).read_bytes(), data)
                self.assertEqual(Path(result["working_copy"]).read_bytes(), data)
                self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(result["size_bytes"], len(data))
                manifest = json.loads(Path(result["manifest_path"]).read_text())
                self.assertEqual(manifest["status"], "verified")
                self.assertEqual(manifest["original_filename"], "original.dat")
                self.assertNotEqual(result["preserved_copy"], result["working_copy"])
                Path(result["working_copy"]).write_bytes(b"changed working copy")
                self.assertEqual(Path(result["preserved_copy"]).read_bytes(), data)
                self.assertEqual(source.read_bytes(), data)

    def test_repeated_preservation_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            first = self.preserve(source, root)
            second = self.preserve(source, root)
            self.assertNotEqual(first["preservation_id"], second["preservation_id"])
            self.assertTrue(Path(first["manifest_path"]).exists())

    def test_repository_destination_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            repository = root / "repository"
            repository.mkdir()
            with patch.object(tool, "REPOSITORY", repository):
                with self.assertRaises(tool.PreservationError):
                    self.preserve(source, repository)
            self.assertEqual(list(repository.iterdir()), [])

    def test_symlink_destination_resolves_into_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            repository = root / "repository"
            repository.mkdir()
            alias = root / "alias"
            alias.symlink_to(repository, target_is_directory=True)
            with patch.object(tool, "REPOSITORY", repository):
                with self.assertRaises(tool.PreservationError):
                    self.preserve(source, alias)

    def test_invalid_sources_and_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            alias = root / "alias"
            alias.symlink_to(source)
            for invalid in (root / "missing", root, alias):
                with self.subTest(source=invalid), self.assertRaises(tool.PreservationError):
                    self.preserve(invalid, root)
            with self.assertRaises(tool.PreservationError):
                self.preserve(source, root / "missing-storage")

    def test_failed_verification_retains_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            storage = root / "storage"
            storage.mkdir()
            with patch.object(tool, "digest_file", return_value=("bad", 0)):
                with self.assertRaises(tool.PreservationError):
                    self.preserve(source, storage)
            folder = next(storage.iterdir())
            self.assertTrue((folder / "FAILED.json").exists())
            self.assertFalse((folder / "manifest.json").exists())
            self.assertTrue((folder / "preserved/content.bin").exists())
            self.assertEqual(source.read_bytes(), b"example")

    def test_source_change_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"example")
            storage = root / "storage"
            storage.mkdir()
            actual_stat = tool.os.stat
            def changed_stat(path, *args, **kwargs):
                if Path(path) == source:
                    source.write_bytes(b"changed")
                return actual_stat(path, *args, **kwargs)
            with patch.object(tool.os, "stat", side_effect=changed_stat):
                with self.assertRaises(tool.PreservationError):
                    self.preserve(source, storage)
            folder = next(storage.iterdir())
            self.assertFalse((folder / "manifest.json").exists())

    def test_received_time_requires_timezone(self):
        with self.assertRaises(tool.argparse.ArgumentTypeError):
            tool.received_timestamp("2026-10-08T12:00:00")
        self.assertEqual(
            tool.received_timestamp("2026-10-08T14:00:00+02:00"), RECEIVED
        )


if __name__ == "__main__":
    unittest.main()
