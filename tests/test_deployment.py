import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


renderer = module("renderer", "deploy/render_nginx.py")
publisher = module("publisher", "scripts/publish_gallery.py")


class DeploymentTests(unittest.TestCase):
    def test_bootstrap_and_https_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            renderer.render(output, "gallery.example.com")
            origin = (output / "emoticon-lookup-origin.conf").read_text()
            front = (output / "emoticon-lookup-front.conf").read_text()
            self.assertIn("listen 127.0.0.1:8088", origin)
            self.assertIn("location / { return 404; }", origin)
            self.assertNotIn("proxy_pass", front)
            self.assertIn("acme-challenge", front)
            renderer.render(output, "gallery.example.com", https=True)
            front = (output / "emoticon-lookup-front.conf").read_text()
            self.assertIn("auth_basic_user_file", front)
            self.assertIn("proxy_pass http://127.0.0.1:8088;", front)
            self.assertIn("proxy_buffering off;", front)
            self.assertIn("/etc/letsencrypt/live/gallery.example.com/fullchain.pem", front)
            self.assertNotIn("@DOMAIN@", front)

    def test_template_values_cannot_inject_directives(self):
        for domain in ("https://example.com", "example.com;", "example.com\nreturn 200;", "../etc", "bad..example", "-bad.example"):
            with self.assertRaises(ValueError):
                renderer.domain_name(domain)
        for path in ("relative/path", "/tmp/../etc", "/tmp/$host", '/tmp/";return 200;', "/tmp/new\nline"):
            with self.assertRaises(ValueError):
                renderer.nginx_path(path)
        for port in (80, 443, 0, 65536):
            with self.assertRaises(ValueError):
                renderer.render(Path("unused"), "gallery.example.com", port=port)

    def test_origin_only_rejects_stale_frontend(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            renderer.render(output, "gallery.example.com")
            with self.assertRaises(ValueError):
                renderer.render(output, "gallery.example.com", origin_only=True)
            fresh = output / "origin-only"
            renderer.render(fresh, "gallery.example.com", origin_only=True)
            self.assertEqual([path.name for path in fresh.iterdir()], ["emoticon-lookup-origin.conf"])

    def test_publish_only_html_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first, second = base / "first.html", base / "second.html"
            first.write_text("<!doctype html><html>first</html>")
            second.write_text("<!doctype html><html>second</html>")
            (base / "messages.json").write_text('{"private": "not for serving"}')
            web = base / "site/www"
            target, digest, size = publisher.publish(first, web)
            self.assertEqual(target.read_bytes(), first.read_bytes())
            self.assertEqual(size, first.stat().st_size)
            self.assertEqual(len(digest), 64)
            publisher.publish(second, web)
            previous = web.parent / "state/previous.html"
            self.assertEqual(previous.read_bytes(), first.read_bytes())
            publisher.publish(previous, web)
            self.assertEqual(target.read_bytes(), first.read_bytes())
            self.assertEqual(previous.read_bytes(), second.read_bytes())
            self.assertEqual([path.name for path in web.iterdir()], ["index.html"])

    def test_invalid_source_preserves_current_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            good, bad = base / "good.html", base / "bad.html"
            good.write_text("<html>complete</html>")
            bad.write_text("<html>unfinished")
            target, _, _ = publisher.publish(good, base / "www")
            with self.assertRaises(ValueError):
                publisher.publish(bad, base / "www")
            self.assertEqual(target.read_bytes(), good.read_bytes())
            self.assertEqual(len(list((base / "www").iterdir())), 1)

    def test_rejects_json_and_public_rollback_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input.json"
            source.write_text("<html>not an HTML filename</html>")
            with self.assertRaises(ValueError):
                publisher.publish(source, base / "www")
            source = source.with_suffix(".html")
            source.write_text("<html>complete</html>")
            with self.assertRaises(ValueError):
                publisher.publish(source, base / "www", base / "www/backups")

    def test_failed_final_replace_leaves_current_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "first.html"
            source.write_text("<html>first</html>")
            target, _, _ = publisher.publish(source, base / "www")
            source.write_text("<html>second</html>")
            original_replace = publisher.os.replace

            def fail_target(source, destination):
                if Path(destination) == target:
                    raise OSError("simulated deployment failure")
                return original_replace(source, destination)

            with patch.object(publisher.os, "replace", side_effect=fail_target):
                with self.assertRaises(OSError):
                    publisher.publish(source, base / "www")
            self.assertEqual(target.read_text(), "<html>first</html>")
            self.assertEqual([path.name for path in (base / "www").iterdir()], ["index.html"])


if __name__ == "__main__":
    unittest.main()
