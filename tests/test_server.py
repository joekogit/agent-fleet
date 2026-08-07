import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from fleet.model import Collector
from fleet import server as server_module
from fleet.server import make_handler, serve


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Collector()))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def test_api_returns_fleet_json(self):
        response = self.get("/api/fleet")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        payload = json.loads(response.read())
        for key in ("generated_at", "counts", "total", "cards"):
            self.assertIn(key, payload)

    def test_cards_match_the_contract(self):
        payload = json.loads(self.get("/api/fleet").read())
        for card in payload["cards"]:
            for key in ("id", "source", "name", "status", "status_reason",
                        "tokens", "subagents_running", "subagents_done"):
                self.assertIn(key, card)

    def test_index_is_served(self):
        self.assertEqual(self.get("/").status, 200)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../../../../etc/passwd")
        self.assertIn(ctx.exception.code, (403, 404))

    def test_path_traversal_to_existing_sibling_file_is_rejected(self):
        # /static/../server.py resolves to fleet/server.py, which exists on
        # disk. Unlike the /etc/passwd case above (which 404s regardless of
        # whether the guard fires, since that path doesn't exist relative to
        # this checkout), this target is guaranteed to exist just outside
        # STATIC_DIR. If the escape guard were missing or broken, this
        # request would succeed with 200 and leak fleet/server.py's source.
        # A bare 404 here would mean the guard silently stopped firing.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../server.py")
        self.assertEqual(ctx.exception.code, 403)

    def request_with_host(self, host, path="/api/fleet"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": host})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_foreign_host_header_is_rejected(self):
        """DNS rebinding defence.

        Loopback binding does not help once an attacker's domain resolves to
        127.0.0.1: the browser sends the request here and treats the reply as
        same-origin. The daemon runs at login on a fixed port, so the window
        never closes, and the payload carries session titles, working
        directories, git branches and Bash command strings.
        """
        status, body = self.request_with_host("evil.example.com")
        self.assertEqual(status, 403)
        self.assertNotIn(b"cards", body)

    def test_foreign_host_header_is_rejected_on_static_too(self):
        status, _ = self.request_with_host("evil.example.com", path="/")
        self.assertEqual(status, 403)

    def test_loopback_host_headers_are_accepted(self):
        for host in (
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
            "127.0.0.1",
            "localhost",
            f"[::1]:{self.port}",
        ):
            with self.subTest(host=host):
                status, body = self.request_with_host(host)
                self.assertEqual(status, 200)
                self.assertIn(b"cards", body)

    def test_loopback_name_on_the_wrong_port_is_rejected(self):
        # A rebinding attacker controls the port their victim connects to,
        # so a Host naming a different port is not this server.
        status, _ = self.request_with_host(f"localhost:{self.port + 1}")
        self.assertEqual(status, 403)


class ServeBindingTest(unittest.TestCase):
    """The hardest constraint in the project is the bind address.

    The suite above binds 127.0.0.1 itself in setUpClass, so it asserts the
    test scaffolding, not the production default. These pin serve() itself.
    """

    def _serve(self, **kwargs):
        bound = []

        class FakeServer:
            def __init__(self, address, handler):
                bound.append(address)

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

        with patch.object(server_module, "ThreadingHTTPServer", FakeServer), \
                patch.object(server_module, "Collector", MagicMock()):
            serve(**kwargs)
        return bound

    def test_default_bind_is_loopback(self):
        self.assertEqual(self._serve(port=0)[0], ("127.0.0.1", 0))

    def test_wildcard_host_is_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                with self.assertRaises(SystemExit):
                    self._serve(port=0, host=host)

    def test_explicit_loopback_host_is_allowed(self):
        self.assertEqual(self._serve(port=0, host="localhost")[0], ("localhost", 0))


if __name__ == "__main__":
    import urllib.error
    unittest.main()
