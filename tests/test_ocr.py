import unittest

from receipt_bot.ocr import extract_full_text, image_mime_type


class ExtractFullTextTests(unittest.TestCase):
    def test_prefers_provider_full_text_and_preserves_line_breaks(self) -> None:
        response = {
            "result": {
                "textAnnotation": {
                    "fullText": "Молоко 154,99\nИТОГО 154,99\n",
                    "blocks": [{"lines": [{"text": "ignored fallback"}]}],
                }
            }
        }
        self.assertEqual(extract_full_text(response), "Молоко 154,99\nИТОГО 154,99\n")

    def test_falls_back_to_lines_in_provider_order(self) -> None:
        response = {
            "result": {
                "textAnnotation": {
                    "blocks": [
                        {"lines": [{"text": "Молоко"}, {"text": "154,99"}]},
                        {"lines": [{"text": "ИТОГО"}]},
                    ]
                }
            }
        }
        self.assertEqual(extract_full_text(response), "Молоко\n154,99\nИТОГО")

    def test_empty_or_unexpected_annotation_returns_empty_text(self) -> None:
        self.assertEqual(extract_full_text({"result": {}}), "")


class ImageMimeTypeTests(unittest.TestCase):
    def test_supported_extensions(self) -> None:
        self.assertEqual(image_mime_type("receipt.JPG"), ("JPEG", ".jpg"))
        self.assertEqual(image_mime_type("receipt.png"), ("PNG", ".png"))
        self.assertEqual(image_mime_type("receipt.pdf"), ("application/pdf", ".pdf"))

    def test_unsupported_extension(self) -> None:
        self.assertIsNone(image_mime_type("receipt.docx"))


if __name__ == "__main__":
    unittest.main()
