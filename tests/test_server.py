import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from fleet.model import Collector
from fleet.server import make_handler


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


if __name__ == "__main__":
    import urllib.error
    unittest.main()
