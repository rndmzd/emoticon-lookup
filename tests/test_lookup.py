import base64
from collections import Counter
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lookup", ROOT / "emoticon_lookup.py")
lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup)


def args(**overrides):
    defaults = dict(user="socksandsmiles", user_field="object.user.username",
                    message_field="object.message.message", starts_only=False, input=None)
    return SimpleNamespace(**(defaults | overrides))


def animated_bytes():
    data = BytesIO()
    Image.new("RGB", (60, 60), "red").save(data, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (60, 60), "blue")], duration=[150, 150], loop=0)
    return data.getvalue()


class LookupTests(unittest.TestCase):
    def test_tokens_anywhere_repeated_and_whitespace(self):
        self.assertEqual(lookup.extract_messages([" :one\t:two\n:one", "text :three", "http://x a:b : nope", None]),
                         Counter(one=2, two=1, three=1))

    def test_starts_only_filters_source_not_subsequent_tokens(self):
        self.assertEqual(lookup.extract_messages(["text :one", "  :two text :three", ": no"], starts_only=True),
                         Counter(two=1, three=1))

    def test_punctuation_and_case_preserved(self):
        self.assertEqual(lookup.extract_messages([":Case :case :name, :étoile :<hello>"]),
                         Counter({"Case": 1, "case": 1, "name,": 1, "étoile": 1, "<hello>": 1}))

    def test_exact_username_filter_and_method_filter(self):
        pipeline = lookup.mongo_pipeline(args(user="socks.and+smiles"))
        match = pipeline[0]["$match"]
        regex = match["object.user.username"]["$regex"]
        self.assertEqual(regex, r"^socks\.and\+smiles$")
        self.assertEqual(match["method"]["$in"], ["chatMessage", "privateMessage"])
        self.assertNotIn("$push", repr(pipeline))

    def test_grouped_export_filters_to_user(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            path.write_text(json.dumps([{"_id": "SOCKSANDSMILES", "messages": [":one", ":one"]},
                                       {"_id": "socksandsmiles2", "messages": [":wrong"]}]))
            self.assertEqual(lookup.collect(args(input=path)), Counter(one=2))

    def test_raw_export_filters_user_and_method(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            rows = [{"method": method, "object": {"user": {"username": user}, "message": {"message": message}}}
                    for method, user, message in [("chatMessage", "socksandsmiles", ":one"),
                        ("privateMessage", "SOCKSANDSMILES", "text :two"), ("tip", "socksandsmiles", ":wrong"),
                        ("chatMessage", "socksandsmiles2", ":wrong")]]
            path.write_text(json.dumps(rows))
            self.assertEqual(lookup.collect(args(input=path)), Counter(one=1, two=1))

    def test_autocomplete_never_substitutes_prefix(self):
        payload = {"emoticons": [{"slug": "etextraryan", "url": "https://example.org/a.gif"}]}
        self.assertIsNone(lookup.exact_media(payload, "etextrary"))
        self.assertEqual(lookup.exact_media(payload, "etextraryan"), "https://example.org/a.gif")

    def test_unexpected_payload_detected(self):
        for value in ([], {}, {"emoticons": "oops"}):
            with self.assertRaises(ValueError):
                lookup.exact_media(value, "one")

    def test_animation_thumbnail_and_original_bytes(self):
        data = animated_bytes()
        png, animated = lookup.thumbnail(data)
        self.assertTrue(animated)
        self.assertTrue(png.startswith(b"\x89PNG"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "original.media"
            path.write_bytes(data)
            embedded = lookup.data_url(path)
            self.assertTrue(embedded.startswith("data:image/gif;base64,"))
            decoded = base64.b64decode(embedded.split(",", 1)[1])
            self.assertEqual(decoded, data)
            self.assertEqual(Image.open(BytesIO(decoded)).n_frames, 2)

    def test_cache_success_and_refresh_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            session = Mock()
            session.get.return_value.json.return_value = {"emoticons": [{"slug": "one", "url": "https://example.org/a.gif"}]}
            with patch.object(lookup, "download", return_value=animated_bytes()):
                first = lookup.resolve(session, "one", "rndmzd", cache)
            self.assertEqual(first["status"], "ok")
            session.reset_mock()
            self.assertEqual(lookup.resolve(session, "one", "rndmzd", cache)["status"], "ok")
            session.get.assert_not_called()
            session.get.return_value.json.return_value = {"emoticons": []}
            self.assertEqual(lookup.resolve(session, "one", "rndmzd", cache, refresh=True)["status"], "missing")
            self.assertEqual(len(list(cache.glob("*.json"))), 0)

    def test_html_is_self_contained_and_escapes_user_content(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            media = folder / "a.media"
            preview = folder / "a.png"
            media.write_bytes(animated_bytes())
            preview.write_bytes(lookup.thumbnail(media.read_bytes())[0])
            rows = [dict(slug='x"><script>bad()</script>', count=2, status="ok", animated=True,
                         url="https://example.org/a.gif", preview=str(preview), media=str(media))]
            output = folder / "gallery.html"
            lookup.write_html(output, rows, '<b>user</b>__ROOM__', "rndmzd")
            html = output.read_text(encoding="utf-8")
            self.assertIn("data:image/gif;base64,", html)
            self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", html)
            self.assertNotIn("<script>bad()", html)
            self.assertNotIn(str(folder), html)
            self.assertIn("&lt;b&gt;user&lt;/b&gt;__ROOM__", html)
            self.assertNotIn("<script src=", html)

    def test_empty_output_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            lookup.write_html(folder / "empty.html", [], "user", "room")
            lookup.write_pdf(folder / "empty.pdf", [], "user", "room")
            pdf = PdfReader(folder / "empty.pdf")
            self.assertEqual(len(pdf.pages), 1)
            self.assertIn("No emoticons matched", pdf.pages[0].extract_text())

    def test_pdf_pagination_names_and_missing_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [dict(slug=f"name{i:02}", count=1, status="missing") for i in range(31)]
            output = Path(folder) / "gallery.pdf"
            lookup.write_pdf(output, rows, "user", "room")
            pdf = PdfReader(output)
            self.assertEqual(len(pdf.pages), 3)
            text = "".join(page.extract_text() for page in pdf.pages)
            self.assertIn(":name30", text)
            self.assertIn("No exact API match", text)
            self.assertEqual(sum(len(page.get("/Annots", [])) for page in pdf.pages), 31)


if __name__ == "__main__":
    unittest.main()
