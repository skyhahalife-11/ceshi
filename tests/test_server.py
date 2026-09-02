"""界面接口的验收：令牌保护、各接口行为、界面文件本身。"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request

from tests.helpers import Sandbox, write_json
from tests.test_flows import cc_settings, make_engine

from suture import server as S


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Suture-Token", token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def _post(url, payload, token=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if token:
        req.add_header("X-Suture-Token", token)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


class TestServer(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox().__enter__()
        write_json(cc_settings(self.sb), {"env": {
            "ANTHROPIC_BASE_URL": self.sb.base_url + "/v1",     # 有问题，可修
            "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
            "ANTHROPIC_MODEL": "glm-5.3",
        }})
        self.httpd, self.state, self.url = S.serve_in_background(make_engine(self.sb))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.sb.__exit__(None, None, None)

    def test_only_listens_on_loopback(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_requests_without_token_are_refused(self):
        for path in ("/", "/api/state"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _get(self.base + path)
            self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _post(self.base + "/api/check", {})
        self.assertEqual(cm.exception.code, 403)

    def test_wrong_token_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _get(self.base + "/api/state", token="not-the-token")
        self.assertEqual(cm.exception.code, 403)

    def test_ui_served_with_token_substituted(self):
        status, html = _get(self.base + "/?token=" + self.state.token)
        self.assertEqual(status, 200)
        self.assertIn(self.state.token, html)
        self.assertNotIn("__SUTURE_TOKEN__", html)

    def test_check_then_fix_flow(self):
        status, state = _get(self.base + "/api/state", token=self.state.token)
        self.assertEqual(json.loads(state)["installed"][0]["id"], "claude_code")

        _, report = _post(self.base + "/api/check", {}, token=self.state.token)
        self.assertEqual(report["result"], "issues")
        self.assertEqual(report["harnesses"][0]["harness_id"], "claude_code")

        _, fixed = _post(self.base + "/api/fix", {"harness_id": "claude_code"},
                         token=self.state.token)
        self.assertEqual(fixed["result"], "fixed", fixed.get("message"))
        after = json.load(open(cc_settings(self.sb), encoding="utf-8"))["env"]
        self.assertEqual(after["ANTHROPIC_BASE_URL"], self.sb.base_url)

    def test_fix_before_check_is_rejected_clearly(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _post(self.base + "/api/fix", {"harness_id": "claude_code"}, token=self.state.token)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("先执行检查", json.loads(cm.exception.read())["error"])

    def test_key_never_leaves_through_api(self):
        secret = "yotta_pk_valid1234"
        _, report = _post(self.base + "/api/check", {}, token=self.state.token)
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))

    def test_apply_choice_endpoint(self):
        # 这份配置本来就有模型（glm-5.3），改测「模型没配置」这种有候选菜单的场景，
        # 单独另开一个 sandbox 更干净。
        with Sandbox() as sb2:
            write_json(cc_settings(sb2), {"env": {
                "ANTHROPIC_BASE_URL": sb2.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
            }})
            httpd, state, _ = S.serve_in_background(make_engine(sb2))
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                _, report = _post(base + "/api/check", {}, token=state.token)
                findings = report["harnesses"][0]["findings"]
                model_finding = next(f for f in findings if f["key"] == "model")
                self.assertTrue(model_finding["choices"])
                chosen = next(c for c in model_finding["choices"] if c["label"] == "glm-5.3")

                _, result = _post(base + "/api/apply_choice", {
                    "harness_id": "claude_code",
                    "field": model_finding["fix_field"],
                    "value": chosen["value"],
                }, token=state.token)
                self.assertEqual(result["result"], "fixed", result.get("message"))
                after = json.load(open(cc_settings(sb2), encoding="utf-8"))["env"]
                self.assertEqual(after["ANTHROPIC_MODEL"], "glm-5.3")
            finally:
                httpd.shutdown()
                httpd.server_close()


class TestUiAsset(unittest.TestCase):
    def test_ui_file_exists_and_has_no_external_dependency(self):
        path = os.path.join(S.UI_DIR, "index.html")
        self.assertTrue(os.path.exists(path))
        html = open(path, encoding="utf-8").read()
        for bad in ("http://", "https://", "cdn.", "<script src"):
            self.assertNotIn(bad, html, f"界面不应该依赖外部资源：{bad}")
        self.assertIn("--accent: #D97757", html)      # Claude 的赤陶主色
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("prefers-reduced-motion", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
