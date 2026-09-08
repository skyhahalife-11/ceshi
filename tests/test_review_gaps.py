"""补 review 指出的四类测试盲区。

这几条不是补充覆盖率，而是「让已经修掉的 Bug 不能再悄悄回来」：

1. 持久化环境变量这条路径原来一条测试都没有——B1、B3、B5 都出在这条路径上。
   真实实现只在 Windows 上有（写注册表），所以这里用一个内存里的假存储替换掉
   读/写/删三个方法：要验证的是 Suture 自己的分流、清理和回滚逻辑，不是注册表。
2. 原来只断言「文件内容变了」「重验证通了」，没有断言「同一条问题真的不再出现」——
   B2 那种「报了修复成功、其实什么都没改」正是被这个漏掉的。
3. 假网关原来两个端点收同一份请求，报文形态错了在测试里看不见——B4 因此隐形。
4. 同一个逻辑字段同时有多条修复建议的场景没有用例——这正是 B1 的形状。
"""
from __future__ import annotations

import os
import unittest
from typing import Dict, Optional
from unittest import mock

from tests.helpers import Sandbox, write_json, write_text

from suture import engine as E
from suture.checks import FIXABLE_NO, FIXABLE_YES, Finding
from suture.engine import Engine
from suture.harness import get_adapter
from suture.harness.base import FIELD_AUTH, FIELD_BASE_URL

CC_ADAPTER = "suture.harness.claude_code.ClaudeCodeAdapter"


def cc_settings(sb) -> str:
    return os.path.join(sb.home, ".claude", "settings.json")


def make_engine(sb, **env_extra) -> Engine:
    return Engine(profile_path=sb.profile_path, env=dict(sb.env, **env_extra),
                  home=sb.home, project_dir=sb.project)


class FakePersistentEnv:
    """内存里的「注册表」。构造时给一份初始值，之后 Suture 的读/写/删都落在这里，
    测完直接看这份字典就知道它到底改了哪个变量、有没有把多余的那个删掉。"""

    def __init__(self, initial: Optional[Dict[str, str]] = None):
        self.store: Dict[str, str] = dict(initial or {})
        self.writes: list = []
        self.unsets: list = []

    def read(self, name: str) -> Optional[str]:
        return self.store.get(name)

    def write(self, name: str, value: str) -> None:
        self.writes.append((name, value))
        self.store[name] = value

    def unset(self, name: str) -> None:
        self.unsets.append(name)
        self.store.pop(name, None)

    def patches(self, supported: bool = True):
        return (
            mock.patch(f"{CC_ADAPTER}.read_persistent_env", side_effect=self.read,
                       autospec=False),
            mock.patch(f"{CC_ADAPTER}.write_persistent_env", side_effect=self.write,
                       autospec=False),
            mock.patch(f"{CC_ADAPTER}.unset_persistent_env", side_effect=self.unset,
                       autospec=False),
            mock.patch(f"{CC_ADAPTER}.supports_persistent_env", return_value=supported,
                       autospec=False),
        )

    def __enter__(self):
        self._active = [p for p in self.patches()]
        for p in self._active:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._active:
            p.stop()
        return False


class TestEnvVarTargets(unittest.TestCase):
    """B3：改的必须是「当前真的在生效的那个变量」，不能写死 ANTHROPIC_API_KEY。"""

    def setUp(self):
        self.adapter = get_adapter("claude_code")

    def _read(self, sb, env):
        return self.adapter.read(env=env, home=sb.home, project_dir=sb.project,
                                 known_keys=[],
                                 accepted_headers=["x-api-key", "Authorization", "Token"])

    def test_auth_token_in_effect_is_the_variable_that_gets_changed(self):
        with Sandbox() as sb:
            env = dict(sb.env, ANTHROPIC_AUTH_TOKEN="  yotta_pk_abcdefgh  ")
            cfg = self._read(sb, env)
            self.assertEqual(cfg.auth_header, "Authorization")
            self.assertEqual(cfg.auth_env_var, "ANTHROPIC_AUTH_TOKEN")
            targets = self.adapter.env_var_targets(cfg, {FIELD_AUTH: "yotta_pk_abcdefgh"})
            # 关键：不能凭空新建 ANTHROPIC_API_KEY——那会把请求头从 Authorization
            # 悄悄换成 x-api-key，而带空格的原值还留在原地
            self.assertEqual(targets, {"ANTHROPIC_AUTH_TOKEN": "yotta_pk_abcdefgh"})
            self.assertNotIn("ANTHROPIC_API_KEY", targets)

    def test_both_variables_set_means_keep_one_and_delete_the_other(self):
        with Sandbox() as sb:
            env = dict(sb.env, ANTHROPIC_API_KEY="yotta_pk_aaaaaaaa",
                       ANTHROPIC_AUTH_TOKEN="yotta_pk_bbbbbbbb")
            cfg = self._read(sb, env)
            self.assertEqual(cfg.auth_rival_env_vars, ["ANTHROPIC_AUTH_TOKEN"])
            targets = self.adapter.env_var_targets(cfg, {FIELD_AUTH: "yotta_pk_aaaaaaaa"})
            self.assertEqual(targets["ANTHROPIC_API_KEY"], "yotta_pk_aaaaaaaa")
            # None 代表「这个变量要删掉」，不是「不用管」
            self.assertIn("ANTHROPIC_AUTH_TOKEN", targets)
            self.assertIsNone(targets["ANTHROPIC_AUTH_TOKEN"])

    def test_auth_from_config_file_does_not_touch_environment_variables(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {"ANTHROPIC_API_KEY": "yotta_pk_filefile"}})
            cfg = self._read(sb, dict(sb.env))
            self.assertIsNone(cfg.auth_env_var)
            self.assertEqual(self.adapter.env_var_targets(cfg, {FIELD_AUTH: "yotta_pk_new"}), {})


class TestPersistentEnvFixFlow(unittest.TestCase):
    """B3/B5：走完整 fix() 流程，看落到「注册表」上的到底是什么。"""

    def test_fix_writes_the_effective_variable_and_removes_the_duplicate(self):
        with Sandbox() as sb:
            initial = {"ANTHROPIC_BASE_URL": sb.base_url,
                       "ANTHROPIC_API_KEY": "  yotta_pk_valid1234  ",
                       "ANTHROPIC_AUTH_TOKEN": "yotta_pk_leftover9"}
            with FakePersistentEnv(initial) as fake:
                eng = make_engine(sb, **initial)
                h = eng.run(["claude_code"]).harnesses[0]
                result = eng.fix("claude_code", h.findings)

                self.assertEqual(result["result"], E.RESULT_FIXED, result["message"])
                # 生效的那个变量被改成去掉空格后的值
                self.assertEqual(fake.store["ANTHROPIC_API_KEY"], "yotta_pk_valid1234")
                # 重复的那个真的被删掉了，不是写回它自己
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", fake.store)
                self.assertIn("ANTHROPIC_AUTH_TOKEN", fake.unsets)

    def test_failed_fix_restores_every_environment_variable_it_touched(self):
        """回滚必须把删掉的变量也恢复回来，不只是写回被改的那个。"""
        with Sandbox(behavior="auth_error") as sb:
            initial = {"ANTHROPIC_BASE_URL": sb.base_url,
                       "ANTHROPIC_API_KEY": "  yotta_pk_valid1234  ",
                       "ANTHROPIC_AUTH_TOKEN": "yotta_pk_leftover9"}
            with FakePersistentEnv(initial) as fake:
                eng = make_engine(sb, **initial)
                h = eng.run(["claude_code"]).harnesses[0]
                result = eng.fix("claude_code", h.findings)

                self.assertEqual(result["result"], E.RESULT_ROLLED_BACK, result["message"])
                self.assertEqual(fake.store, initial)   # 一个不多一个不少，值也一样

    def test_platform_without_support_reports_instead_of_raising(self):
        """B5：改持久化环境变量只在 Windows 上实现，别的平台上要如实说明，
        不能抛未捕获的 RuntimeError（原来 engine 只 catch OSError）。"""
        with Sandbox() as sb:
            env = {"ANTHROPIC_BASE_URL": "https://wrong.example",
                   "ANTHROPIC_API_KEY": "yotta_pk_valid1234"}
            fake = FakePersistentEnv(dict(env))
            patches = fake.patches(supported=False)
            for p in patches:
                p.start()
            try:
                eng = make_engine(sb, **env)
                h = eng.run(["claude_code"]).harnesses[0]
                result = eng.fix("claude_code", h.findings)   # 不能抛异常
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(result["result"], E.RESULT_MANUAL)
            self.assertIn("ANTHROPIC_BASE_URL", result["message"])
            self.assertIn("环境变量", result["message"])
            self.assertEqual(fake.writes, [])     # 什么都没动
            self.assertEqual(fake.unsets, [])


class TestFixClosesTheLoop(unittest.TestCase):
    """B2：修完之后再检查一次，同一条问题必须真的不再出现。
    只断言「文件变了」「请求通了」是不够的——那两条在空转的修复上也成立。"""

    def test_wrong_address_is_gone_on_the_next_check(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",       # 多拼了一段
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3"}})
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            self.assertFalse(next(f for f in h.findings if f.key == "base_url").ok)

            eng.fix("claude_code", h.findings)

            again = make_engine(sb).run(["claude_code"]).harnesses[0]
            self.assertTrue(next(f for f in again.findings if f.key == "base_url").ok)
            self.assertEqual(again.result, E.RESULT_HEALTHY)

    def test_findings_that_claim_fixable_actually_disappear(self):
        """凡是标了「能自动修」的项，修完再查都不该原样再报一次。
        标了 FIXABLE_NO 的（比如多层不一致）不在此列——那种本来就是让用户自己去删。"""
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url + "/v1",
                "ANTHROPIC_AUTH_TOKEN": "  yotta_pk_valid1234  ",
                "ANTHROPIC_MODEL": "GLM-5.3"}})
            eng = make_engine(sb)
            h = eng.run(["claude_code"]).harnesses[0]
            claimed = {f.key for f in h.findings if f.fixable == FIXABLE_YES}
            self.assertTrue(claimed, "这个用例本身要先造出可自动修复的项")

            eng.fix("claude_code", h.findings)

            again = make_engine(sb).run(["claude_code"]).harnesses[0]
            still_bad = {f.key for f in again.findings if not f.ok}
            self.assertEqual(claimed & still_bad, set(),
                             f"这些项报了能自动修复，但修完还在：{claimed & still_bad}")


class TestWireFormatIsHarnessSpecific(unittest.TestCase):
    """B4：Codex 走 OpenAI 形态，不该带 Anthropic 协议特有的 anthropic-version 头。
    假网关现在会校验形态，形态发错会被判 400，所以这条能真的抓住回归。"""

    def test_codex_request_uses_openai_shape(self):
        with Sandbox() as sb:
            write_text(os.path.join(sb.home, ".codex", "config.toml"),
                       'model = "kimi-k3"\nmodel_provider = "ai-gate"\n\n'
                       '[model_providers.ai-gate]\n'
                       f'base_url = "{sb.base_url}/v1"\n'
                       'env_key = "AI_GATE_API_KEY"\n')
            h = make_engine(sb, AI_GATE_API_KEY="yotta_pk_valid1234").run(["codex"]).harnesses[0]
            self.assertTrue(h.e2e_results, "Codex 应该真的发了一次端到端请求")
            self.assertTrue(h.e2e_results[0]["ok"], h.e2e_results[0])

    def test_claude_code_request_still_uses_anthropic_shape(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3"}})
            h = make_engine(sb).run(["claude_code"]).harnesses[0]
            self.assertTrue(h.e2e["ok"], h.e2e)

    def test_headers_differ_by_style(self):
        from suture import gateway
        anthropic = gateway._headers({"x-api-key": "k"}, "anthropic")
        openai = gateway._headers({"x-api-key": "k"}, "openai")
        self.assertIn("anthropic-version", anthropic)
        self.assertNotIn("anthropic-version", openai)


class TestSameFieldFixConflict(unittest.TestCase):
    """B1：同一个逻辑字段有多条修复建议时，不能让 dict 赋值的先后决定结果。"""

    @staticmethod
    def _finding(key, value, label="x"):
        return Finding(key=key, label=label, ok=False, detail="",
                       fixable=FIXABLE_YES, fix_field=FIELD_BASE_URL, fix_value=value)

    def test_correction_wins_over_alignment(self):
        correct = self._finding("base_url", "https://right.example", "网关地址")
        align = self._finding("conflict:base_url", "https://wrong.example", "多层配置取值不一致")
        for order in ([correct, align], [align, correct]):   # 顺序不该影响结果
            changes, notes, unresolved = E._merge_changes(order)
            self.assertEqual(changes, {FIELD_BASE_URL: "https://right.example"})
            self.assertEqual(unresolved, [])
            self.assertTrue(notes, "跳过了哪一条要如实说明")

    def test_two_equal_suggestions_that_disagree_are_left_alone(self):
        a = self._finding("base_url", "https://a.example", "网关地址")
        b = self._finding("model", "https://b.example", "模型名称")   # 同为纠正类
        changes, _, unresolved = E._merge_changes([a, b])
        self.assertEqual(changes, {})            # 不猜，不写
        self.assertTrue(unresolved)
        self.assertIn("不自动修改", unresolved[0])

    def test_engine_reports_manual_when_nothing_can_be_decided(self):
        with Sandbox() as sb:
            a = self._finding("base_url", "https://a.example", "网关地址")
            b = self._finding("model", "https://b.example", "模型名称")
            result = make_engine(sb).fix("claude_code", [a, b])
            self.assertEqual(result["result"], E.RESULT_MANUAL)
            self.assertIn("人工确认", result["message"])

    def test_whitespace_fix_is_not_undone_by_the_conflict_fix(self):
        """带空格的 Key + 鉴权填在两个变量里：去空格那条必须赢。"""
        with Sandbox() as sb:
            env = dict(sb.env, ANTHROPIC_BASE_URL=sb.base_url,
                       ANTHROPIC_API_KEY="  yotta_pk_valid1234  ",
                       ANTHROPIC_AUTH_TOKEN="yotta_pk_leftover9")
            eng = Engine(profile_path=sb.profile_path, env=env,
                         home=sb.home, project_dir=sb.project)
            h = eng.check("claude_code")
            changes, _, _ = E._merge_changes(h.findings)
            self.assertEqual(changes.get(FIELD_AUTH), "yotta_pk_valid1234")


class TestMixedHarnessSummary(unittest.TestCase):
    """次要问题：一个 harness 正常、另一个还没配置过时，总结论不能说「全部通过」。"""

    def test_partly_unconfigured_is_not_reported_as_all_passed(self):
        with Sandbox() as sb:
            write_json(cc_settings(sb), {"env": {
                "ANTHROPIC_BASE_URL": sb.base_url,
                "ANTHROPIC_API_KEY": "yotta_pk_valid1234",
                "ANTHROPIC_MODEL": "glm-5.3"}})
            os.makedirs(os.path.join(sb.home, ".codex"), exist_ok=True)   # 装了但没配
            report = make_engine(sb).run(["claude_code", "codex"])
            self.assertNotIn("检查全部通过", report.message)
            self.assertIn("还没配置过", report.message)
            self.assertIn("Codex", report.message)


if __name__ == "__main__":
    unittest.main()
