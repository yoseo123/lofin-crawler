import json
import tempfile
import unittest
from pathlib import Path

from src.chunked_writer import ChunkedPolicyWriter


class ChunkedPolicyWriterTests(unittest.TestCase):
    def test_writes_parts_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            writer = ChunkedPolicyWriter(
                output_path=Path(td) / "2016 전국 정책.md",
                output_format="md",
                chunk_size=2,
                source="local",
                default_year="2016-01-01",
                default_region="전국",
            )

            for idx in range(5):
                writer.add_row({"사업명": f"정책{idx + 1}", "지역": "전국", "회계연도": "2016"})

            result = writer.close()
            manifest = json.loads((Path(td) / result.manifest_file).read_text(encoding="utf-8"))

            self.assertEqual(result.count, 5)
            self.assertEqual(result.chunks, 3)
            self.assertEqual(manifest["total_count"], 5)
            self.assertTrue(manifest["complete"])
            self.assertEqual(len(manifest["chunks"]), 3)
            self.assertTrue((Path(td) / "2016 전국 정책_part001.md").exists())
            self.assertTrue((Path(td) / "2016 전국 정책_part001.json").exists())
            self.assertTrue((Path(td) / "2016 전국 정책_part003.md").exists())

    def test_close_can_mark_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            writer = ChunkedPolicyWriter(
                output_path=Path(td) / "out.md",
                output_format="md",
                chunk_size=10,
                source="local",
            )
            writer.add_row({"사업명": "정책", "지역": "전국", "회계연도": "2026"})

            result = writer.close(complete=False)
            manifest = json.loads((Path(td) / result.manifest_file).read_text(encoding="utf-8"))

            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["total_count"], 1)


if __name__ == "__main__":
    unittest.main()
