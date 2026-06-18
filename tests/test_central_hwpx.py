import io
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from src.central_hwpx import (
    DownloadSpec,
    extract_business_content_from_text,
    extract_download_specs,
    extract_hwpx_text,
)


class CentralHwpxTests(unittest.TestCase):
    def _build_hwpx_zip(self, files: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as zf:
            for name, payload in files.items():
                zf.writestr(name, payload)
        return buffer.getvalue()

    def test_extract_download_specs_from_download_call(self):
        html = """
        <script>
          function fn_download(){
            $.download('FILE123', '2', '세부사업.hwpx');
          }
        </script>
        """

        got = extract_download_specs(html, {})

        self.assertEqual(got[0], DownloadSpec(file_id="FILE123", file_seq="2", file_name="세부사업.hwpx"))

    def test_extract_download_specs_from_row_token(self):
        row = {"sayBrkdFileNm": "ATCH001|7|detail.hwpx"}

        got = extract_download_specs("", row)

        self.assertEqual(got[0], DownloadSpec(file_id="ATCH001", file_seq="7", file_name="detail.hwpx"))

    def test_extract_download_specs_from_files_path(self):
        html = '<a href="/files/ATCH999/3?name=%EC%84%B8%EB%B6%80.hwpx">download</a>'

        got = extract_download_specs(html, {})

        self.assertEqual(got[0], DownloadSpec(file_id="ATCH999", file_seq="3", file_name="세부.hwpx"))

    def test_extract_hwpx_text_prefers_preview(self):
        payload = self._build_hwpx_zip({"Preview/PrvText.txt": "사업내용\n청년 고용 지원".encode("utf-8")})

        text = extract_hwpx_text(payload)

        self.assertIn("사업내용", text)
        self.assertIn("청년 고용 지원", text)

    def test_extract_hwpx_text_from_section_xml(self):
        xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<hp:section xmlns:hp=\"http://www.hancom.co.kr/hwpml/2016/format\">
  <hp:p><hp:run><hp:t>사업내용</hp:t></hp:run></hp:p>
  <hp:p><hp:run><hp:t>청년 취업을 위한 맞춤형 교육을 지원한다.</hp:t></hp:run></hp:p>
</hp:section>
""".encode("utf-8")
        payload = self._build_hwpx_zip({"Contents/section0.xml": xml})

        text = extract_hwpx_text(payload)

        self.assertIn("사업내용", text)
        self.assertIn("청년 취업", text)

    def test_extract_business_content_from_text_section(self):
        raw = """
        제목
        사업내용: 청년 취업을 위한 맞춤형 교육과 멘토링을 지원한다.
        추진근거: 청년고용촉진 특별법
        """

        got = extract_business_content_from_text(raw)

        self.assertEqual(got, "청년 취업을 위한 맞춤형 교육과 멘토링을 지원한다.")

    def test_extract_hwpx_text_rejects_oversized_entry(self):
        payload = self._build_hwpx_zip(
            {"Contents/section0.xml": b"A" * ((10 * 1024 * 1024) + 1)}
        )

        text = extract_hwpx_text(payload)

        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
