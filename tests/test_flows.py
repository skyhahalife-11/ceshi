"""端到端验收：跑完整的三个阶段，验证每条分支的行为和对用户文件的影响。"""
from __future__ import annotations

import json
import os
import unittest

from tests.helpers import Sandbox, write_json, write_text

from suture import engine as E
from suture.engine import Engine


def cc_settings(sb) -> str:
    return os.path.join(sb.home, ".claude", "settings.json")


def make_engine(sb, **env_extra) -> Engine:
    return Engine(profile_path=sb.profile_path, env=dict(sb.env, **env_extra),
                  home=sb.home, project_dir=sb.project)


class TestHealthyPath(unittest.TestCase):
    def test_correct_config_passes_end_to_end(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            self.assertEqual(report.result, E.RESULT_HEALTHY)
            h = report.harnesses[0]
            self.assertTrue(h.e2e["ok"])
            self.assertEqual([f.label for f in h.findings if not f.ok], [])


class TestFixSuccess(unittest.TestCase):
    def test_three_problems_fixed_and_verified(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",       # 多拼了一段 /v1
                "ANTHROPIC_AUTH_TOKEN": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "GLM-5.3",                    # 大小写不对
            }})
            eng = make_engine(sb)
            report = eng.run(["claude_code"])
            self.assertEqual(report.result, E.RESULT_ISSUES)
            h = report.harnesses[0]
            self.assertFalse(h.e2e["ok"])          # 修复前端到端是不通的

            result = eng.fix("claude_code", h.findings)
            self.assertEqual(result["result"], E.RESULT_FIXED, result.get("message"))

            after = json.load(open(cc_settings(sb), encoding="utf-8"))["env"]
            self.assertEqual(after["ANTHROPIC_BASE_URL"], sb.base_url)
            self.assertEqual(after["ANTHROPIC_MODEL"], "glm-5.3")
            self.assertIn("重新打开", result["message"])

            self.assertEqual(make_engine(sb).run(["claude_code"]).result, E.RESULT_HEALTHY)

    def test_fix_creates_recoverable_backup(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            before = open(cc_settings(sb), encoding="utf-8").read()
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            result = eng.fix("claude_code", h.findings)
            backup_dir = result["backup_dir"]
            self.assertTrue(os.path.isdir(backup_dir))
            saved = [os.path.join(backup_dir, n) for n in os.listdir(backup_dir)]
            self.assertTrue(any(open(p, encoding="utf-8").read() == before for p in saved))
            for p in saved:      # 备份里有 Key 明文，权限必须收紧
                self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")


class TestGatewayDown(unittest.TestCase):
    def test_gateway_failure_stops_before_touching_config(self):
        for behavior in ("server_error", "rate_limited"):
            with self.subTest(behavior=behavior), Sandbox(behavior=behavior) as sb:
                write_json(cc_settings(sb), {"env": {
                    "ANTHROPIC_BASE_URL": sb.base_url + "/v1",   # 本地确实有问题
                    "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                    "ANTHROPIC_MODEL": "glm-5.3",
                }})
                before = open(cc_settings(sb), encoding="utf-8").read()
                report = make_engine(sb).run(["claude_code"])
                self.assertEqual(report.result, E.RESULT_GATEWAY_DOWN)
                self.assertEqual(report.harnesses, [])          # 根本没进阶段二
                self.assertEqual(open(cc_settings(sb), encoding="utf-8").read(), before)

    def test_unreachable_gateway_is_network_error(self):
        with Sandbox() as sb:
            port = sb.server.server_address[1]
            sb.server.shutdown()
            sb.server.server_close()             # 连监听套接字一起关，制造真正的连不上
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}/zi/proxy",
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            self.assertEqual(report.gateway_probe["classification"], "network_error")
            self.assertEqual(report.result, E.RESULT_GATEWAY_DOWN)

    def test_auth_error_is_not_treated_as_gateway_side(self):
        """401 更可能是本地 Key 配错了，正是这个工具要抓的场景，
        不能因为探活没返回 200 就一律甩锅给网关。"""
        with Sandbox(behavior="auth_error") as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "sk-ant-somebodys-own-key",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            self.assertEqual(report.gateway_probe["classification"], "auth_error")
            self.assertNotEqual(report.result, E.RESULT_GATEWAY_DOWN)
            self.assertTrue(report.harnesses)                    # 继续查了本地配置
            labels = [f.label for f in report.harnesses[0].findings if not f.ok]
            self.assertIn("鉴权信息来源", labels)


class TestAttribution(unittest.TestCase):
    def test_gateway_breaks_after_fix_keeps_the_fix(self):
        """修完重验证不通，但补测发现是网关侧的问题：保留修复，不回滚。"""
        with Sandbox(behavior="flaky_after:2") as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            result = eng.fix("claude_code", h.findings)
            self.assertEqual(result["result"], E.RESULT_KEPT_FIX, result.get("message"))
            self.assertIn("不是这次修复导致的", result["message"])
            after = json.load(open(cc_settings(sb), encoding="utf-8"))["env"]
            self.assertEqual(after["ANTHROPIC_BASE_URL"], sb.base_url)

    def test_fix_that_does_not_help_is_rolled_back(self):
        """网关正常、Key 本身就是无效的：修完仍不通，回滚。"""
        with Sandbox(behavior="auth_error") as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",
                "ANTHROPIC_API_KEY": "yotta_pk_invalid99",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            before = open(cc_settings(sb), encoding="utf-8").read()
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            result = eng.fix("claude_code", h.findings)
            self.assertEqual(result["result"], E.RESULT_ROLLED_BACK, result.get("message"))
            self.assertNotIn("rollback_failed", result)
            self.assertEqual(open(cc_settings(sb), encoding="utf-8").read(), before)


class TestFreshUser(unittest.TestCase):
    def test_no_config_generates_instead_of_reporting_problems(self):
        for harness, tail in (("claude_code", ".claude/settings.json"),
                              ("codex", ".codex/config.toml"),
                              ("deepseek", ".dsh/settings.yaml")):
            with self.subTest(harness=harness), Sandbox() as sb:
                eng = make_engine(sb)
                rep = eng.check(harness)
                self.assertEqual(rep.result, E.RESULT_NO_CONFIG)
                path = eng.generate_config(harness)
                self.assertTrue(os.path.exists(path))
                self.assertTrue(path.endswith(os.path.normpath(tail)), path)
                text = open(path, encoding="utf-8").read()
                self.assertIn(sb.base_url, text)
                if harness == "codex":     # Codex 需要地址自带 /v1
                    self.assertIn(sb.base_url + "/v1", text)

    def test_generated_deepseek_credentials_file_is_private(self):
        with Sandbox() as sb:
            make_engine(sb).generate_config("deepseek")
            creds = os.path.join(sb.home, ".dsh", ".credentials.yaml")
            self.assertTrue(os.path.exists(creds))
            self.assertEqual(oct(os.stat(creds).st_mode)[-3:], "600")


class TestMultiHarness(unittest.TestCase):
    def test_each_installed_harness_reported_separately(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3"}})
            write_text(os.path.join(sb.home, ".codex", "config.toml"),
                       'model = "kimi-k3"\nmodel_provider = "ai-gate"\n\n'
                       '[model_providers.ai-gate]\n'
                       f'base_url = "{sb.base_url}"\n'          # 少了 /v1
                       'env_key = "AI_GATE_API_KEY"\n')
            report = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234").run()
            ids = {h.harness_id: h for h in report.harnesses}
            self.assertEqual(set(ids), {"claude_code", "codex"})
            self.assertEqual(ids["claude_code"].result, E.RESULT_HEALTHY)
            self.assertEqual(ids["codex"].result, E.RESULT_ISSUES)
            self.assertTrue(any("少了一段 /v1" in f.detail
                                for f in ids["codex"].findings if not f.ok))

    def test_no_harness_detected_is_reported_honestly(self):
        with Sandbox() as sb:
            report = make_engine(sb).run()
            self.assertEqual(report.result, E.RESULT_NO_HARNESS)
            self.assertIn("还没有覆盖", report.message)


class TestCodexFlow(unittest.TestCase):
    def test_fix_writes_user_layer_when_project_untrusted(self):
        with Sandbox() as sb:
            write_text(os.path.join(sb.home, ".codex", "config.toml"),
                       'model = "glm-5.3"\nmodel_provider = "ai-gate"\n\n'
                       '[model_providers.ai-gate]\n'
                       f'base_url = "{sb.base_url}"\n'
                       'env_key = "AI_GATE_API_KEY"\n')
            project_cfg = os.path.join(sb.project, ".codex", "config.toml")
            write_text(project_cfg, 'model = "kimi-k3"\n')
            before_project = open(project_cfg, encoding="utf-8").read()

            eng = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234")
            h = eng.run(["codex"]).harnesses[0]
            result = eng.fix("codex", h.findings)
            self.assertEqual(result["result"], E.RESULT_FIXED, result.get("message"))
            # 不可信的项目层不该被改——改了也不生效
            self.assertEqual(open(project_cfg, encoding="utf-8").read(), before_project)
            user_text = open(os.path.join(sb.home, ".codex", "config.toml"), encoding="utf-8").read()
            self.assertIn(sb.base_url + "/v1", user_text)


class TestDeepSeekFlow(unittest.TestCase):
    def test_fix_writes_user_layer_and_leaves_baseline_alone(self):
        with Sandbox() as sb:
            write_text(os.path.join(sb.project, "cordis.yml"),
                       "plugins:\n"
                       "  - name: '@deepseek-ai/dsh-llm-pi-ai'\n"
                       "    config:\n"
                       "      providers:\n"
                       "        tower-ai:\n"
                       "          api: anthropic-messages\n"
                       f"          baseURL: {sb.base_url}/v1\n"      # 多了一段 /v1
                       "          apiKeyEnv: AI_GATE_API_KEY\n"
                       "          models:\n"
                       "            - id: glm-5.3\n")
            eng = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234")
            h = eng.run(["deepseek"]).harnesses[0]
            self.assertEqual(h.result, E.RESULT_ISSUES)
            result = eng.fix("deepseek", h.findings)
            self.assertEqual(result["result"], E.RESULT_FIXED, result.get("message"))
            settings = os.path.join(sb.home, ".dsh", "settings.yaml")
            self.assertTrue(os.path.exists(settings))
            self.assertIn(sb.base_url, open(settings, encoding="utf-8").read())
            # 基线文件随项目分发，不该由本机工具改动
            self.assertIn(f"{sb.base_url}/v1",
                          open(os.path.join(sb.project, "cordis.yml"), encoding="utf-8").read())


class TestSecrecy(unittest.TestCase):
    def test_key_never_appears_in_report(self):
        secret = "yotta_pk_supersecret"
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",
                "ANTHROPIC_API_KEY": secret,
                "ANTHROPIC_MODEL": "glm-5.3"}})
            report = make_engine(sb).run(["claude_code"])
            blob = json.dumps(E.report_to_dict(report), ensure_ascii=False, default=str)
            self.assertNotIn(secret, blob)
            self.assertNotIn("supersecret", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPhaseOneTargetsTheGatewayItself(unittest.TestCase):
    """阶段一要回答的是「网关本身活着吗」，所以必须探规则里那个已知正确的地址，
    而不是用户可能填错的那个——否则用户把地址填成一个不存在的域名时，
    工具会连不上，然后判定成「网关侧问题，不要动你的配置」，
    但真正的问题恰恰就是本地这个地址填错了，正是该修的东西。"""

    def test_wrong_local_address_is_not_blamed_on_the_gateway(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/nonexistent",   # 连不上的地址
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            self.assertNotEqual(report.result, E.RESULT_GATEWAY_DOWN,
                                "网关是好的，不该判成网关侧问题")
            self.assertEqual(report.gateway_probe["classification"], "ok")
            h = report.harnesses[0]
            self.assertTrue(any(f.label == "网关地址" and not f.ok for f in h.findings))

    def test_real_gateway_outage_still_detected_even_if_local_config_fine(self):
        with Sandbox(behavior="server_error") as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            self.assertEqual(report.result, E.RESULT_GATEWAY_DOWN)


class TestCustomHeaderAuthEndToEnd(unittest.TestCase):
    """真实报过的场景：鉴权只靠 ANTHROPIC_CUSTOM_HEADERS 里的 Token 头，
    完全没设 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN。之前 Suture 压根不认识
    这个变量，会误判成「没有配置鉴权信息」；现在应该能正常识别、跑通。"""

    def test_token_only_via_custom_headers_is_recognized_and_healthy(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_CUSTOM_HEADERS": "Token: yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            h = report.harnesses[0]
            self.assertTrue(h.e2e["ok"], h.e2e)
            self.assertEqual(report.result, E.RESULT_HEALTHY)
            self.assertFalse(any(not f.ok and "没有配置鉴权信息" in f.detail
                                 for f in h.findings))

    def test_stale_api_key_alongside_working_custom_header_is_flagged_but_not_silently_fixed(self):
        # API_KEY 是旧的、不对的值；真正在用的是 CUSTOM_HEADERS 里的 Token。
        # 两个头都会被真实客户端发出去，假网关按 x-api-key 优先，所以这里
        # 应该复现用户真实遇到的那个 401——但 Suture 现在应该能明确指出
        # 「两处都在生效、内容不一样」，而不是笼统地归因成「没配鉴权」。
        accepted_headers_env = "Token: yotta_pk_valid1234"
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "sk-ant-stale-old-key",
                "ANTHROPIC_CUSTOM_HEADERS": accepted_headers_env,
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            h = report.harnesses[0]
            self.assertFalse(h.e2e["ok"])   # 假网关按 x-api-key 优先，复现真实的 401
            conflict = next(f for f in h.findings if f.key == "auth-multiple-active")
            self.assertFalse(conflict.ok)
            self.assertIn("Token", conflict.detail)


class TestE2eOverridesStaticConnectivityFindings(unittest.TestCase):
    """端到端真实请求已经证明「当前这套地址/鉴权确实连得通」时（比如走了本地转发），
    跟连通性直接相关的静态判断不该继续说「这是错的」——尤其不能让一键修复照着
    静态规则去改一个已经在正常工作的配置。"""

    def test_nonstandard_but_reachable_base_url_is_not_treated_as_broken(self):
        with Sandbox() as sb:
            profile = json.load(open(sb.profile_path, encoding="utf-8"))
            # 规则文件里登记的地址和实际配置的不一样，但实际配置的这个是真的能连通
            # （现实中往往是走了本地转发/代理）。
            profile["base_url"]["canonical_root"] = sb.base_url + "/via-local-relay"
            with open(sb.profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False)
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3",
            }})
            report = make_engine(sb).run(["claude_code"])
            h = report.harnesses[0]
            self.assertTrue(h.e2e["ok"], h.e2e)
            base_url_finding = next(f for f in h.findings if f.key == "base_url")
            self.assertTrue(base_url_finding.ok, base_url_finding.detail)
            self.assertEqual(base_url_finding.fixable, "no")
            self.assertIn("端到端真实请求已经验证", base_url_finding.detail)
            self.assertEqual(report.result, E.RESULT_HEALTHY)
            # 一键修复不该再把这个字段当成待办
            self.assertEqual(h.fixable_count, 0)


class TestApplyChoice(unittest.TestCase):
    """「有候选值、但不该替用户猜」的场景（模型没配置、名字像别的型号）：
    用户从菜单里点了哪个，Suture 就把哪个写进去——跟批量「一键修复」是两条独立的路，
    而且要走同一套备份/重新验证/失败回滚的安全流程。"""

    def test_choosing_from_full_list_when_model_is_unconfigured(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                # 没设 ANTHROPIC_MODEL
            }})
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            model_finding = next(f for f in h.findings if f.key == "model")
            self.assertEqual(model_finding.fixable, "no")   # 不是批量一键修复能碰的
            chosen = next(c for c in model_finding.choices if c["label"] == "glm-5.3")

            result = eng.apply_choice("claude_code", model_finding.fix_field, chosen["value"])
            self.assertEqual(result["result"], E.RESULT_FIXED, result.get("message"))
            after = json.load(open(cc_settings(sb), encoding="utf-8"))["env"]
            self.assertEqual(after["ANTHROPIC_MODEL"], "glm-5.3")

    def test_choosing_one_deepseek_model_preserves_the_others(self):
        with Sandbox() as sb:
            write_text(os.path.join(sb.project, "cordis.yml"),
                       "plugins:\n"
                       "  - name: '@deepseek-ai/dsh-llm-pi-ai'\n"
                       "    config:\n"
                       "      providers:\n"
                       "        tower-ai:\n"
                       "          api: anthropic-messages\n"
                       f"          baseURL: {sb.base_url}\n"
                       "          apiKeyEnv: AI_GATE_API_KEY\n"
                       "          models:\n"
                       "            - id: glm-5.3\n"
                       "            - id: gpt-5.6-so\n"          # 像 gpt-5.6-sol 但不确定选哪个
                       "            - id: kimi-k3\n")
            eng = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234")
            h = eng.run(["deepseek"]).harnesses[0]
            ambiguous = next(f for f in h.findings if f.key == "model:gpt-5.6-so")
            self.assertEqual(ambiguous.fixable, "no")
            chosen = next(c for c in ambiguous.choices if c["label"] == "gpt-5.6-sol")

            result = eng.apply_choice("deepseek", ambiguous.fix_field, chosen["value"])
            self.assertEqual(result["result"], E.RESULT_FIXED, result.get("message"))
            settings = open(os.path.join(sb.home, ".dsh", "settings.yaml"),
                           encoding="utf-8").read()
            # 选中的那个改对了，另外两个原样保留，不能因为改一个就把清单清空
            self.assertIn("glm-5.3", settings)
            self.assertIn("gpt-5.6-sol", settings)
            self.assertIn("kimi-k3", settings)
            self.assertNotIn("gpt-5.6-so\n", settings)


class TestMultiModelEndToEnd(unittest.TestCase):
    """DeepSeek Harness 的模型是一份注册清单。网关路由表本身就是可信名单，
    一个模型名字是不是网关真的支持，靠 check_models() 拿去跟路由表做静态比对
    就够了——不需要为每一个注册的模型都真发一次请求，那样又慢又要花真实调用的钱。
    真实请求只用来验证「地址 + 鉴权本身通不通」，挑一个已确认有效的名字测一次就够。"""

    def test_only_one_real_request_is_sent_even_with_several_models_registered(self):
        with Sandbox() as sb:
            write_text(os.path.join(sb.project, "cordis.yml"),
                       "plugins:\n"
                       "  - name: '@deepseek-ai/dsh-llm-pi-ai'\n"
                       "    config:\n"
                       "      providers:\n"
                       "        tower-ai:\n"
                       "          api: anthropic-messages\n"
                       f"          baseURL: {sb.base_url}\n"
                       "          apiKeyEnv: AI_GATE_API_KEY\n"
                       "          models:\n"
                       "            - id: glm-5.3\n"
                       "            - id: kimi-k3\n"
                       "            - id: not-a-real-model\n")
            eng = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234")
            h = eng.run(["deepseek"]).harnesses[0]
            # 只发了一次真实请求，挑的是第一个已经确认在路由表里的名字
            self.assertEqual(len(h.e2e_results), 1)
            self.assertEqual(h.e2e_results[0]["model"], "glm-5.3")
            self.assertTrue(h.e2e_results[0]["ok"], h.e2e_results[0])
            # 但每个注册的名字仍然各自做了静态校验，该报的问题一个都不少
            model_findings = {f.current_value: f.ok for f in h.findings
                              if f.key.startswith("model:")}
            self.assertEqual(model_findings.get("glm-5.3"), True)
            self.assertEqual(model_findings.get("kimi-k3"), True)
            self.assertFalse(any(f.ok for f in h.findings
                                 if f.key == "model:not-a-real-model"))
