"""流程编排。命令行和图形界面调用的是同一套引擎，不存在两份判断逻辑。

主线：阶段一网关自检 → 阶段二 harness 体检 → 阶段三一键修复。
阶段一没通过就直接停下，不动本地配置；修复后重验证失败时补测网关做归因，
判断该保留修复还是回滚。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import fixer, gateway
from .checks import Finding, FIXABLE_NO, FIXABLE_YES, run_all_checks
from .harness import ALL_ADAPTERS, detect_installed, get_adapter
from .harness.base import FIELD_AUTH, FIELD_BASE_URL, FIELD_MODEL, HarnessConfig, mask_secret
from .profile import expected_base_url, load_profile

STAGE_GATEWAY = "gateway"
STAGE_HARNESS = "harness"
STAGE_FIX = "fix"

# 整体结论
RESULT_HEALTHY = "healthy"                  # 全部正常
RESULT_ISSUES = "issues"                    # 发现问题，可以修
RESULT_GATEWAY_DOWN = "gateway_down"        # 网关侧问题，不碰本地配置
RESULT_NO_CONFIG = "no_config"              # 全新用户，没配置过
RESULT_NO_HARNESS = "no_harness"            # 没探测到支持的 harness
RESULT_MANUAL = "manual"                    # 有问题但没有能自动修的
RESULT_FIXED = "fixed"
RESULT_KEPT_FIX = "kept_fix"                # 修复保留，当前连不上是网关的问题
RESULT_ROLLED_BACK = "rolled_back"


@dataclass
class HarnessReport:
    harness_id: str
    display_name: str
    result: str
    findings: List[Finding] = field(default_factory=list)
    infos: List[str] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)
    e2e: Optional[Dict[str, Any]] = None            # 兼容旧界面：等于 e2e_results[0]
    e2e_results: List[Dict[str, Any]] = field(default_factory=list)
    fixable_count: int = 0
    generated_path: str = ""


@dataclass
class Report:
    profile_source: str
    gateway_probe: Optional[Dict[str, Any]] = None
    result: str = ""
    message: str = ""
    harnesses: List[HarnessReport] = field(default_factory=list)


def _wire(profile: Dict[str, Any], harness_id: str) -> Dict[str, str]:
    """这个 harness 真实会发的请求路径和报文形态。"""
    table = profile.get("per_harness_request", {})
    entry = table.get(harness_id) or {"suffix": "/v1/messages", "style": "anthropic"}
    return {"suffix": entry.get("suffix", "/v1/messages"),
            "style": entry.get("style", "anthropic")}


def _probe_dict(p: gateway.ProbeResult) -> Dict[str, Any]:
    return {"ok": p.ok, "classification": p.classification, "status": p.status,
            "detail": p.detail, "elapsed_ms": p.elapsed_ms}


def _auth_headers(cfg: Optional[HarnessConfig], fallback_key: Optional[str] = None) -> Dict[str, str]:
    """按这个 harness 真实客户端的行为拼出这次请求应该带的鉴权请求头——
    主字段（API_KEY/AUTH_TOKEN 二选一解析出来的那个）加上所有平行生效的自定义头
    一起带上，不替用户猜网关到底认哪一个。只有当两者都没有时才退回到「随便找一个
    已知能用的 Key」去探活，用来顺带验证网关本身通不通。"""
    headers: Dict[str, str] = {}
    if cfg is not None:
        value = cfg.field(FIELD_AUTH).value
        if value:
            if cfg.auth_header == "Authorization":
                headers["Authorization"] = f"Bearer {value}"
            else:
                headers[cfg.auth_header] = value
        for extra in cfg.extra_auth_headers:
            if extra.value:
                headers.setdefault(extra.header, extra.value)
    if not headers and fallback_key:
        headers["x-api-key"] = fallback_key
    return headers


# 端到端结果如果证明「当前这套地址 + 鉴权确实连得通」，这几类静态判断就不该
# 再说「这是错的」——它们本来就是在没有更强证据时候的猜测。
_CONNECTIVITY_FINDING_KEYS = {"base_url", "auth", "auth-ref"}


class Engine:
    def __init__(self, profile_path: Optional[str] = None, env=None,
                 home: Optional[str] = None, project_dir: Optional[str] = None):
        self.profile, self.profile_source = load_profile(profile_path)
        self.env = env if env is not None else os.environ
        self.home = home
        self.project_dir = project_dir or os.getcwd()
        self._configs: Dict[str, HarnessConfig] = {}
        self._backups: Dict[str, fixer.BackupManifest] = {}

    # ---- 读取 ----
    def read_harness(self, harness_id: str) -> HarnessConfig:
        adapter = get_adapter(harness_id)
        cfg = adapter.read(env=self.env, home=self.home, project_dir=self.project_dir,
                           known_keys=self.profile.get("known_settings_keys", []),
                           accepted_headers=self.profile.get("auth", {}).get("accepted_headers", []))
        self._configs[harness_id] = cfg
        return cfg

    def installed(self):
        return detect_installed(env=self.env, home=self.home, project_dir=self.project_dir)

    # ---- 阶段一 ----
    def probe(self, cfg: Optional[HarnessConfig] = None,
              override_base_url: Optional[str] = None,
              override_key: Optional[str] = None) -> gateway.ProbeResult:
        """阶段一要回答的是「网关本身活着吗」，所以探的是规则里那个已知正确的地址，
        而不是用户配置里那个可能填错的地址——否则用户把地址填错时，工具会连不上，
        然后判定成「网关侧问题，不要动本地配置」，而真正该修的恰恰就是这个地址。
        用户配置里的 Key 会带上：拿它探活能顺带看出 Key 本身是不是有效的。

        override_key 是用户在探活前自己确认/改写的 Key——用它是因为 Suture 读到的
        环境变量/配置文件里的值，不一定就是这次真的要跑的那把（比如注册表里的持久化值
        和当前终端继承的值没同步）。有 override 时完全不用 cfg 里的值，避免两者混着猜。"""
        probe_model = self.profile.get("probe_model")
        base_url = override_base_url or expected_base_url(self.profile, "claude_code")
        wire = _wire(self.profile, "claude_code")
        if override_key:
            # 手填的 Key，不知道用户的真实客户端会用哪种请求头带它，
            # 规则里 accepted_headers 列出的都是网关认的位置，全部带上，
            # 不用去猜是哪一种——跟 check_auth() 里"网关两个头都认"是同一个道理。
            accepted = self.profile.get("auth", {}).get("accepted_headers") or ["x-api-key"]
            headers = {
                h: (f"Bearer {override_key}" if h == "Authorization" else override_key)
                for h in accepted
            }
        else:
            headers = _auth_headers(cfg, fallback_key=self._any_known_key())
        return gateway.probe_gateway(base_url, headers, probe_model,
                                     suffix=wire["suffix"], style=wire["style"])

    def _any_known_key(self) -> Optional[str]:
        """从已经读到的任意一个 harness 配置里取一个 Key 用于探活。"""
        for cfg in self._configs.values():
            value = cfg.field(FIELD_AUTH).value
            if value:
                return value
        return None

    def probe_defaults(self) -> Dict[str, Any]:
        """给探活前的确认卡片用：地址是规则里那个固定正确值，Key 从已安装的
        harness 里找一个已配置的——两者都只是「默认填入」，用户改了就按用户的走。"""
        base_url = expected_base_url(self.profile, "claude_code")
        key = None
        key_source = ""
        for a in self.installed():
            cfg = self.read_harness(a.harness_id)
            rf = cfg.field(FIELD_AUTH)
            if rf.is_set:
                key, key_source = rf.value, rf.source_layer
                break
        return {
            "base_url": base_url,
            "has_key": bool(key),
            "api_key_masked": mask_secret(key) if key else "",
            "api_key_source": key_source,
        }

    # ---- 阶段二 ----
    def check(self, harness_id: str) -> HarnessReport:
        adapter = get_adapter(harness_id)
        cfg = self.read_harness(harness_id)
        rep = HarnessReport(harness_id=harness_id, display_name=adapter.display_name, result="")
        rep.files = [{"layer": f.layer, "path": f.path, "exists": f.exists,
                      "parse_ok": f.parse_ok, "active": f.active,
                      "inactive_reason": f.inactive_reason} for f in cfg.files]

        if not cfg.has_any_config:
            rep.result = RESULT_NO_CONFIG
            return rep

        rep.findings = run_all_checks(cfg, self.profile)
        rep.infos = list(cfg.notes)

        # 端到端真实请求：有两类问题只做静态检查发现不了——
        # 一类是「字段看起来没错，但网关不认这个值」；
        # 另一类刚好相反，「字段跟网关登记的对不上，但其实是通的」（比如走了本地转发）。
        # 这一步只用来验证「当前这套地址 + 鉴权本身通不通」，不是逐个验证每个模型——
        # 网关路由表本身就是真实、可信的名单（profile.models），一个名字是不是
        # 网关真的支持，靠 check_models() 拿去跟这份名单做静态比对就够了，不需要
        # 为每一个注册的模型都真发一次请求，那样既慢又要花真实调用的钱。真实请求
        # 只挑一个「已经确认是网关真实模型」的名字来测，测的是连通性本身；如果一个
        # 模型的名字压根不在路由表里，check_models() 已经会报出来、需要用户自己确认
        # 换成哪一个，不需要再靠一次真实请求去发现同一件事。
        base_url = cfg.field(FIELD_BASE_URL).value
        headers = _auth_headers(cfg)
        wire = _wire(self.profile, harness_id)
        if base_url:
            if cfg.model_candidates:
                valid = [f.current_value for f in rep.findings
                        if f.key.startswith("model:") and f.ok and f.current_value]
                target = valid[0] if valid else (
                    cfg.model_candidates[0] if cfg.model_candidates
                    else self.profile.get("probe_model"))
            else:
                target = cfg.field(FIELD_MODEL).value or self.profile.get("probe_model")
            e2e = gateway.send_real_request(base_url, headers, target,
                                            suffix=wire["suffix"], style=wire["style"])
            rep.e2e_results.append({"model": target, **_probe_dict(e2e)})
        rep.e2e = rep.e2e_results[0] if rep.e2e_results else None

        # 只要有一个模型真的连通了，就证明当前这套地址 + 鉴权组合本身没问题——
        # 静态规则跟这个证据冲突时，让证据说了算，尤其不能让一键修复照着静态规则
        # 去改一个已经在正常工作的配置。
        if any(r["ok"] for r in rep.e2e_results):
            for f in rep.findings:
                if not f.ok and f.key in _CONNECTIVITY_FINDING_KEYS:
                    f.ok = True
                    f.fixable = FIXABLE_NO
                    f.fix_field = None
                    f.fix_value = None
                    f.detail += ("（端到端真实请求已经验证当前配置能正常连通网关，可能是走了本地转发/"
                                "代理，所以没有当成问题处理；如果以后突然连不上了，这里是首先要检查的地方。）")

        issues = [f for f in rep.findings if not f.ok]
        rep.fixable_count = len([f for f in issues if f.fixable == FIXABLE_YES])

        if issues:
            rep.result = RESULT_ISSUES if rep.fixable_count else RESULT_MANUAL
        elif rep.e2e_results and not all(r["ok"] for r in rep.e2e_results):
            rep.result = RESULT_MANUAL
        else:
            rep.result = RESULT_HEALTHY
        return rep

    def generate_config(self, harness_id: str) -> str:
        adapter = get_adapter(harness_id)
        # 这里不能填阶段一探活用的 probe_model——那个模型只是用来验证网关活不活着，
        # 跟用户实际想用哪个模型是两回事，Suture 不知道答案，所以用一个明显需要
        # 替换掉的占位文字，跟 Key 的占位符是同一个道理。下次检查时会在「模型名称」
        # 那一项列出网关支持的完整型号清单，让用户自己选。
        return adapter.generate_minimal_config(
            base_url=expected_base_url(self.profile, harness_id),
            model="把这里换成你要用的模型名称（检查结果里会列出网关支持的型号）",
            env=self.env, home=self.home, project_dir=self.project_dir)

    def apply_choice(self, harness_id: str, field: str, value: str) -> Dict[str, Any]:
        """给「有候选值、但 Suture 不该替用户挑」这类场景用（模型完全没配置、
        或者名字长得像另一个真实型号）：界面把候选值渲染成菜单，用户点了哪个，
        就调这个方法把哪个写进去。跟这条 Finding 本身标没标 fixable=YES 无关——
        只要是用户自己明确选的，就按这个值走一遍 fix() 已经有的完整流程
        （备份 → 写入 → 重新测试 → 失败就回滚），不用另外重写一套安全机制。"""
        synthetic = Finding(key=f"choice:{field}", label="用户选择", ok=False, detail="",
                            fixable=FIXABLE_YES, fix_field=field, fix_value=value)
        return self.fix(harness_id, [synthetic])

    # ---- 阶段三 ----
    def fix(self, harness_id: str, findings: List[Finding]) -> Dict[str, Any]:
        adapter = get_adapter(harness_id)
        cfg = self._configs.get(harness_id) or self.read_harness(harness_id)

        changes: Dict[str, str] = {}
        for f in findings:
            if f.fixable == FIXABLE_YES and f.fix_field and f.fix_value is not None:
                changes[f.fix_field] = f.fix_value
        if not changes:
            return {"result": RESULT_MANUAL, "message": "没有可以自动修复的项。", "applied": []}

        steps: List[Dict[str, str]] = []
        manifest = fixer.backup_files(adapter.writable_paths(cfg), home=self.home)
        # 环境变量的备份必须在 apply_fixes 之前做，且是独立于文件备份的另一套，
        # 因为改的可能根本不是文件——env_var_targets 只在「这个字段现在真的是被
        # 环境变量顶着生效」时才会非空，大多数情况下这里就是空字典。
        manifest.env_entries = fixer.backup_env(adapter, cfg, changes)
        self._backups[harness_id] = manifest
        if manifest.env_entries:
            steps.append({"step": "backup_env",
                          "detail": f"已记录 {len(manifest.env_entries)} 个持久化环境变量的原值，用于失败时回滚"})
        steps.append({"step": "backup", "detail": f"已备份原配置到 {manifest.directory}"})

        try:
            applied = fixer.apply_fixes(adapter, cfg, changes, env=self.env,
                                        home=self.home, project_dir=self.project_dir)
        except OSError as exc:
            # 写入失败必须如实上报，不能显示修复成功但其实什么都没改
            return {"result": RESULT_MANUAL, "applied": [], "steps": steps,
                    "message": f"写入配置失败：{exc}。原配置没有被改动，备份在 {manifest.directory}。"}
        steps.append({"step": "write", "detail": "；".join(applied) if applied else "没有需要写入的改动"})

        # 用修正后的配置重发真实请求验证。如果刚改的是持久化环境变量，当前进程的
        # self.env（os.environ 快照）感知不到刚写的新值——必须把 manifest.env_entries
        # 里涉及的变量换成刚才写入 apply() 时那个持久化存储的实时值，再拿这份「实时
        # 覆盖后的环境」去重新解析配置，否则永远会读到旧值，把一次成功的修复误判成失败。
        live_env = dict(self.env)
        for name in manifest.env_entries:
            live_value = adapter.read_persistent_env(name)
            if live_value is not None:
                live_env[name] = live_value
            else:
                live_env.pop(name, None)
        cfg2 = adapter.read(env=live_env, home=self.home, project_dir=self.project_dir,
                            known_keys=self.profile.get("known_settings_keys", []),
                            accepted_headers=self.profile.get("auth", {}).get("accepted_headers", []))
        self._configs[harness_id] = cfg2

        base_url = cfg2.field(FIELD_BASE_URL).value
        model = (cfg2.model_candidates[0] if cfg2.model_candidates
                 else cfg2.field(FIELD_MODEL).value) or self.profile.get("probe_model")
        wire = _wire(self.profile, harness_id)
        retest = gateway.send_real_request(base_url, _auth_headers(cfg2), model,
                                           suffix=wire["suffix"], style=wire["style"])
        steps.append({"step": "verify", "detail": retest.detail})

        restart_note = ("如果当前已经开着这个客户端，或者有其它已经打开的终端，"
                        "需要重新打开才会看到这次改动——上面这一步验证用的是持久化存储的实时值，"
                        "不是已经打开的窗口那份旧的环境变量快照。") if manifest.env_entries else \
                       "如果当前已经开着这个客户端，需要重新打开一下才会生效。"

        if retest.ok:
            return {"result": RESULT_FIXED, "applied": applied, "steps": steps,
                    "retest": _probe_dict(retest), "backup_dir": manifest.directory,
                    "message": f"修复成功。{restart_note}"}

        # 重发仍失败：补测网关自检做归因，而不是直接判定修复没生效
        reprobe = self.probe(cfg2)
        if reprobe.classification in gateway.GATEWAY_SIDE:
            return {"result": RESULT_KEPT_FIX, "applied": applied, "steps": steps,
                    "retest": _probe_dict(retest), "reprobe": _probe_dict(reprobe),
                    "backup_dir": manifest.directory,
                    "message": "配置已经改好，但目前连不上是网关那边的问题，不是这次修复导致的。"
                               "这次修复保留，没有回滚。"}

        failed = fixer.rollback(manifest)
        failed += fixer.rollback_env(adapter, manifest.env_entries) if manifest.env_entries else []
        if failed:
            return {"result": RESULT_ROLLED_BACK, "applied": applied, "steps": steps,
                    "retest": _probe_dict(retest), "reprobe": _probe_dict(reprobe),
                    "backup_dir": manifest.directory, "rollback_failed": failed,
                    "message": f"这次修复没有解决问题，回滚时以下文件写回失败，需要人工检查：{failed}"}
        return {"result": RESULT_ROLLED_BACK, "applied": applied, "steps": steps,
                "retest": _probe_dict(retest), "reprobe": _probe_dict(reprobe),
                "backup_dir": manifest.directory,
                "message": "网关自检正常，但用修正后的配置仍然连不上：这次修复没有解决问题，"
                           "已经回滚到修复前的配置，建议联系研发进一步排查。"}

    # ---- 完整一轮 ----
    def run(self, harness_ids: Optional[List[str]] = None,
            override_base_url: Optional[str] = None,
            override_key: Optional[str] = None) -> Report:
        report = Report(profile_source=self.profile_source)

        adapters = ([get_adapter(h) for h in harness_ids] if harness_ids else self.installed())
        if not adapters:
            report.result = RESULT_NO_HARNESS
            report.message = ("没有探测到 Claude Code CLI、Codex CLI 或 DeepSeek Harness。"
                              "如果你用的是别的客户端，这一期还没有覆盖。")
            return report

        # 阶段一：用其中一个已配置的 harness 去探活，网关状态对所有 harness 是同一件事。
        # override_base_url/override_key 是用户在确认卡片里自己填的，优先级比读到的配置更高。
        probe_cfg = None
        configs = []
        for a in adapters:
            cfg = self.read_harness(a.harness_id)
            configs.append(cfg)
            if probe_cfg is None and cfg.field(FIELD_BASE_URL).is_set:
                probe_cfg = cfg

        # 零配置直连：本机没有任何网关地址/Key，但至少一个 harness 检测到原生登录/
        # 已有可用凭据——这时候拿空鉴权头去打真实网关必然是 401，那不代表配置有
        # 问题，只是没什么好探的，探了反而会把一个正常状态渲染成吓人的错误横幅。
        skip_probe = (
            not override_base_url and not override_key and probe_cfg is None
            and self._any_known_key() is None
            and any(c.native_login for c in configs)
        )
        if skip_probe:
            report.gateway_probe = {
                "ok": True, "classification": "skipped", "status": None,
                "detail": "检测到零配置直连（原生登录/已有可用凭据），本机没有网关地址/Key 可探测，跳过网关自检。",
                "elapsed_ms": 0,
            }
        else:
            probe = self.probe(probe_cfg, override_base_url=override_base_url, override_key=override_key)
            report.gateway_probe = _probe_dict(probe)

            if probe.classification in gateway.GATEWAY_SIDE:
                report.result = RESULT_GATEWAY_DOWN
                report.message = ("判定为网关侧问题，不会去动本地配置。建议联系网关值班同学，"
                                  "或者稍后重试。")
                return report

        # 阶段二：逐个 harness 体检，分别出结论，不合并成一份笼统的报告
        for a in adapters:
            report.harnesses.append(self.check(a.harness_id))

        if any(h.result == RESULT_ISSUES for h in report.harnesses):
            report.result = RESULT_ISSUES
        elif all(h.result == RESULT_NO_CONFIG for h in report.harnesses):
            report.result = RESULT_NO_CONFIG
            report.message = "没有找到任何配置。这不是配错了，是还没配置过——可以直接生成一份最小配置。"
        elif any(h.result == RESULT_MANUAL for h in report.harnesses):
            report.result = RESULT_MANUAL
            report.message = "发现的问题里没有能自动修复的，需要人工处理。"
        else:
            report.result = RESULT_HEALTHY
            any_e2e = any(h.e2e_results for h in report.harnesses)
            report.message = ("检查全部通过，请求也跑通了。如果还是用不了，问题可能不在客户端配置。"
                              if any_e2e else "检查全部通过，没有发现问题。")
        return report


def report_to_dict(report: Report) -> Dict[str, Any]:
    """报告要能直接序列化给界面。Finding 里的 current_value 在检测层就已经掩码过，
    这里不会再引入明文 Key。"""
    return asdict(report)
