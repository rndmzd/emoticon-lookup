"""Exercise the actual NGINX templates on temporary loopback ports, with test TLS."""
import base64
from hashlib import sha256
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
import unittest
import urllib.error
import urllib.request

from test_deployment import renderer, publisher


def unused_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@unittest.skipUnless(shutil.which("nginx") and shutil.which("openssl"), "NGINX/OpenSSL not installed")
class HostingNginxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="emoticon-nginx-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        ports = set()
        while len(ports) < 3:
            ports.add(unused_port())
        self.origin_port, self.http_port, self.tls_port = sorted(ports)
        self.web = self.base / "www"
        self.acme = self.base / "acme"
        challenge = self.acme / ".well-known/acme-challenge/test-token"
        challenge.parent.mkdir(parents=True)
        challenge.write_text("challenge-data")
        self.source = self.base / "source.html"
        self.source.write_bytes(b"<!doctype html><html>" + b"<!-- synthetic large gallery -->\n" * 100000 + b"</html>")
        publisher.publish(self.source, self.web)
        self.password = self.base / "readers.htpasswd"
        hashed = subprocess.check_output(["openssl", "passwd", "-apr1", "test-password"]).decode().strip()
        self.password.write_text("reader:" + hashed + "\n")
        self.cert, self.key = self.base / "cert.pem", self.base / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=gallery.example.test", "-addext", "subjectAltName=IP:127.0.0.1,DNS:gallery.example.test",
                        "-keyout", str(self.key), "-out", str(self.cert)], check=True, capture_output=True)
        context = ssl.create_default_context(cafile=str(self.cert))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def configure(self, https):
        renderer.render(self.base, "gallery.example.test", port=self.origin_port, web_root=str(self.web),
                        acme_root=str(self.acme), https=https, certificate=str(self.cert),
                        certificate_key=str(self.key), password_file=str(self.password))
        front = self.base / "emoticon-lookup-front.conf"
        text = front.read_text().replace("listen 80;", f"listen 127.0.0.1:{self.http_port};")
        text = text.replace("listen [::]:80;", f"listen [::1]:{self.http_port};")
        text = text.replace("listen 443 ssl;", f"listen 127.0.0.1:{self.tls_port} ssl;")
        text = text.replace("listen [::]:443 ssl;", f"listen [::1]:{self.tls_port} ssl;")
        front.write_text(text)
        configuration = self.base / "nginx.conf"
        configuration.write_text(f'''daemon off;
master_process off;
pid "{self.base}/nginx.pid";
error_log stderr notice;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path "{self.base}/client_temp";
    proxy_temp_path "{self.base}/proxy_temp";
    fastcgi_temp_path "{self.base}/fastcgi_temp";
    uwsgi_temp_path "{self.base}/uwsgi_temp";
    scgi_temp_path "{self.base}/scgi_temp";
    include "{self.base}/emoticon-lookup-origin.conf";
    include "{front}";
}}
''')
        command = ["nginx", "-p", str(self.base) + "/", "-c", str(configuration)]
        subprocess.run(command + ["-t"], check=True, capture_output=True)
        self.log = (self.base / "process.log").open("w")
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop)
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail("NGINX failed to start: " + (self.base / "process.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", self.http_port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("NGINX did not become ready")

    def stop(self):
        if hasattr(self, "process"):
            self.process.terminate()
            self.process.wait(timeout=5)
            self.log.close()

    def request(self, port, path="/", tls=False, authenticated=False):
        headers = {"Host": "gallery.example.test"}
        if authenticated:
            token = base64.b64encode(b"reader:test-password").decode()
            headers["Authorization"] = "Basic " + token
        request = urllib.request.Request(f"{'https' if tls else 'http'}://127.0.0.1:{port}{path}", headers=headers)
        try:
            return self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as response:
            return response

    def test_bootstrap_serves_only_challenges(self):
        self.configure(https=False)
        with self.request(self.http_port) as response:
            self.assertEqual(response.status, 404)
        with self.request(self.http_port, "/.well-known/acme-challenge/test-token") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"challenge-data")
        with self.request(self.origin_port, "/healthz") as response:
            self.assertEqual(response.read(), b"ok\n")

    def test_tls_proxy_auth_large_response_and_republish(self):
        self.configure(https=True)
        with self.request(self.http_port, "/index.html") as response:
            self.assertEqual(response.status, 301)
            self.assertEqual(response.headers["Location"], "https://gallery.example.test/index.html")
        with self.request(self.tls_port, tls=True) as response:
            self.assertEqual(response.status, 401)
        with self.request(self.tls_port, tls=True, authenticated=True) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertEqual(sha256(response.read()).digest(), sha256(self.source.read_bytes()).digest())
        for path in ("/messages.json", "/.env", "/state/previous.html", "/unknown/"):
            with self.request(self.tls_port, path, tls=True, authenticated=True) as response:
                self.assertEqual(response.status, 404)
        self.source.write_text("<!doctype html><html>updated</html>")
        publisher.publish(self.source, self.web)
        with self.request(self.tls_port, tls=True, authenticated=True) as response:
            self.assertEqual(response.read(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
