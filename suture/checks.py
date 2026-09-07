"""检测层。

只针对适配器归一化后的结构做判断，不认任何一个 harness 的原始字段名，
所以同一套判断能覆盖三个 harness。这一层只产出问题清单，不写任何文件。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .harness.base import (
    FIELD_AUTH, FIELD_BASE_URL, FIELD_MODEL, HarnessConfig, mask_secret,
)
from .profile import expected_base_url, model_ids, model_index

FIXABLE_YES = "yes"
FIXABLE_PARTIAL = "partial"
FIXABLE_NO = "no"


@dataclass
class Finding:
    key: str                     # 检测项标识
    label: str                   # 展示名
    ok: bool
    detail: str                  # 一句话说清楚问题是什么
    fixable: str = FIXABLE_NO
    current_value: str = ""      # 展示用，已掩码
    suggested_value: str = ""
    fix_field: Optional[str] = None   # 修复要写的逻辑字段
    fix_value: Optional[str] = None   # 修复要写的值
    note: str = ""
    # 有几个候选值、但 Suture 不该替用户挑哪个时用这个：界面把它们渲染成
    # 可以点的选项，用户点了哪个就把 fix_field 写成对应的 value——跟「一键修复」
    # 批量套用 fixable=YES 是两条不同的路，这条永远需要用户自己点一下。
    # 每一项是 {"label": 给用户看的名字, "value": 真要写进去的完整值}——
    # 两者不一定相同：比如 DeepSeek 一次注册了好几个模型，选中某一个候选时
    # 要写回去的是「其它模型保持不动、只把这一个换掉」之后的完整清单，
    # 不能让 apply_choice 把其它已经注册好的模型顶掉。
    choices: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class InfoItem:
    """仅供参考、不算错误的信息。"""
    text: str


# ---------- 配置文件语法 ----------

def check_config_syntax(cfg: HarnessConfig) -> List[Finding]:
    out: List[Finding] = []
    for fs in cfg.files:
        if not fs.exists:
            continue
        label = f"{fs.layer}语法"
        if not fs.parse_ok:
            out.append(Finding(
                key=f"syntax:{fs.path}", label=label, ok=False,
                detail=fs.parse_error,
                fixable=FIXABLE_NO,
                note=f"文件位置：{fs.path}",
            ))
        else:
            out.append(Finding(
                key=f"syntax:{fs.path}", label=label, ok=True,
                detail="格式没有问题。", note=f"文件位置：{fs.path}"))
    return out


def check_unknown_keys(cfg: HarnessConfig, profile: Dict[str, Any]) -> List[Finding]:
    """字段名拼错是另一种失败方式：文件能正常解析，但这个字段不会被认出来，
    等同于没配置这一项，所以要单独查、单独提示。"""
    known = profile.get("known_settings_keys", [])
    out: List[Finding] = []
    for fs in cfg.files:
        if not (fs.exists and fs.parse_ok and fs.unknown_keys):
            continue
        for key in fs.unknown_keys:
            near = difflib.get_close_matches(key, known, n=1, cutoff=0.7)
            if near:
                out.append(Finding(
                    key=f"unknown-key:{fs.path}:{key}", label=f"{fs.layer}字段名", ok=False,
                    detail=f"字段名 {key} 不是可识别的配置项，看起来是 {near[0]} 拼错了。"
                           "文件本身能正常解析，但这个字段不会被认出来，等同于没配置这一项。",
                    current_value=key, suggested_value=near[0], fixable=FIXABLE_NO,
                    note=f"文件位置：{fs.path}"))
    return out


def check_layer_active(cfg: HarnessConfig) -> List[Finding]:
    """某一层配置存在但实际不会被采纳（Codex 的不可信项目）——
    用户往往正是在改这一层，必须明确告诉他改了没用。"""
    out: List[Finding] = []
    for fs in cfg.files:
        if fs.exists and not fs.active:
            out.append(Finding(
                key=f"inactive-layer:{fs.path}", label=f"{fs.layer}未生效", ok=False,
                detail=fs.inactive_reason, fixable=FIXABLE_NO,
                note=f"文件位置：{fs.path}"))
    return out


# ---------- 网关地址 ----------

def check_base_url(cfg: HarnessConfig, profile: Dict[str, Any]) -> Finding:
    expected = expected_base_url(profile, cfg.harness_id)
    rf = cfg.field(FIELD_BASE_URL)
    label = "网关地址"

    if not rf.is_set:
        return Finding(key="base_url", label=label, ok=False,
                       detail="没有配置网关地址。", suggested_value=expected,
                       fixable=FIXABLE_YES, fix_field=FIELD_BASE_URL, fix_value=expected)

    value = rf.value
    cleaned = value.strip()
    problems: List[str] = []

    if cleaned != value:
        problems.append("地址前后有多余空格")
    if cleaned.startswith("http://") and profile.get("base_url", {}).get("require_https", True):
        cleaned = "https://" + cleaned[len("http://"):]
        problems.append("协议应该是 https，写成了 http")
    elif not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned.lstrip("/")
        problems.append("地址少了 https:// 前缀")

    normalized = cleaned.rstrip("/")
    expected_norm = expected.rstrip("/")

    if normalized != expected_norm:
        # 常见的两种：多拼了一段 /v1，或者少了 /v1
        if normalized == expected_norm + "/v1":
            problems.append("结尾多拼了一段 /v1，这个 harness 会自己拼接后面的路径，不需要手动加")
        elif expected_norm == normalized + "/v1":
            problems.append("结尾少了一段 /v1，这个 harness 需要地址里自带这一段")
        else:
            problems.append(f"地址跟网关登记的不一致，网关这边应该是 {expected_norm}")
        normalized = expected_norm
    elif cleaned.endswith("/") and not profile.get("base_url", {}).get("allow_trailing_slash", True):
        problems.append("结尾多了一个斜杠")

    if not problems:
        return Finding(key="base_url", label=label, ok=True,
                       detail="地址和格式都正确。", current_value=value)

    note = f"当前生效的是{rf.source_layer}里的值"
    if rf.source_layer == "环境变量":
        note += "；修复会改写持久化的环境变量本身，已经打开的终端/客户端需要重新打开才会生效"
    return Finding(key="base_url", label=label, ok=False,
                   detail="；".join(problems) + "。",
                   current_value=value, suggested_value=normalized,
                   fixable=FIXABLE_YES, fix_field=FIELD_BASE_URL, fix_value=normalized,
                   note=note)


# ---------- 鉴权 ----------

def _check_key_prefix(value: str, gate_prefix: str, vendor_table: Dict[str, str]) -> Tuple[bool, str]:
    """按前缀判断像不像网关签发的 Key。返回 (是否像, 不像时的说明)。"""
    if not gate_prefix or value.startswith(gate_prefix):
        return True, ""
    vendor = _guess_vendor(value, vendor_table)
    return False, (f"不是网关签发的 Key（网关的 Key 以 {gate_prefix} 开头）"
                   + (f"，看起来是{vendor}的 Key" if vendor else ""))


def check_auth(cfg: HarnessConfig, profile: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    auth_rules = profile.get("auth", {})
    gate_prefix = auth_rules.get("gateway_issued_key_prefix", "")
    vendor_table = auth_rules.get("known_vendor_key_prefixes", {})
    rf = cfg.field(FIELD_AUTH)
    extras = cfg.extra_auth_headers

    # 间接引用的 harness：先看那个变量名本身取不取得到值
    if cfg.auth_is_indirect:
        if not cfg.auth_env_name:
            out.append(Finding(
                key="auth-ref", label="鉴权引用", ok=False,
                detail="配置里没有写明去哪个环境变量取 Key。",
                suggested_value="AI_GATE_API_KEY",
                fixable=FIXABLE_YES, fix_field=FIELD_AUTH, fix_value="AI_GATE_API_KEY"))
        elif not cfg.auth_env_resolved:
            out.append(Finding(
                key="auth-ref", label="鉴权引用", ok=False,
                detail=f"配置指向 {cfg.auth_env_name}，但这个名字取不到值——"
                       "配置文件本身看起来没问题，实际发请求时拿不到 Key。",
                current_value=cfg.auth_env_name, fixable=FIXABLE_NO,
                note="需要设置这个环境变量，或者把 Key 存进凭据文件；Suture 不会把 Key 明文写进配置文件"))
        else:
            out.append(Finding(
                key="auth-ref", label="鉴权引用", ok=True,
                detail=f"配置指向 {cfg.auth_env_name}，能正常取到值。",
                current_value=cfg.auth_env_name))

    has_primary = rf.is_set
    if not has_primary and not extras:
        if not cfg.auth_is_indirect:
            out.append(Finding(
                key="auth", label="鉴权信息", ok=False,
                detail="没有配置鉴权信息，请求会被网关直接拒绝。", fixable=FIXABLE_NO,
                note="需要去网关后台生成一个 Key"))
        return out

    if has_primary:
        value = rf.value
        if value != value.strip():
            out.append(Finding(
                key="auth-whitespace", label="鉴权信息格式", ok=False,
                detail="Key 前后有多余的空格或换行，多半是复制粘贴时带进来的，会导致请求头不合法。",
                current_value=mask_secret(value), suggested_value="去掉前后空格",
                fixable=FIXABLE_YES, fix_field=FIELD_AUTH, fix_value=value.strip()))
            value = value.strip()

        ok_prefix, why = _check_key_prefix(value, gate_prefix, vendor_table)
        if not ok_prefix:
            out.append(Finding(
                key="auth", label="鉴权信息来源", ok=False, detail=why + "。",
                current_value=mask_secret(value), fixable=FIXABLE_PARTIAL,
                note="换成正确的 Key 需要自己去网关后台重新生成，Suture 不能代劳"))
        else:
            out.append(Finding(
                key="auth", label="鉴权信息", ok=True,
                detail="格式符合网关签发的 Key。", current_value=mask_secret(value),
                note=f"取自{rf.source_layer}" if rf.source_layer else ""))
    elif extras:
        out.append(Finding(
            key="auth", label="鉴权信息", ok=True,
            detail=f"没有走 API_KEY/AUTH_TOKEN 这两个专用字段，但通过 {extras[0].source} "
                   f"设置的 {extras[0].header} 头能当鉴权用。",
            note="下面单独校验这个头本身的内容"))

    # 平行的自定义头：跟主字段是「都会被发出去」的关系，不是互斥关系，
    # 每一个都要单独校验内容像不像网关签发的 Key。
    for extra in extras:
        label = f"鉴权信息（{extra.header} 头）"
        ok_prefix, why = _check_key_prefix(extra.value, gate_prefix, vendor_table)
        if not ok_prefix:
            out.append(Finding(
                key=f"auth-extra:{extra.header}", label=label, ok=False,
                detail=f"来自 {extra.source} 的 {extra.header} 头{why}。",
                current_value=mask_secret(extra.value), fixable=FIXABLE_PARTIAL,
                note="换成正确的 Key 需要自己去网关后台重新生成，Suture 不能代劳"))
        else:
            out.append(Finding(
                key=f"auth-extra:{extra.header}", label=label, ok=True,
                detail=f"来自 {extra.source}，格式符合网关签发的 Key。",
                current_value=mask_secret(extra.value)))

    if cfg.auth_conflict:
        out.append(Finding(
            key="auth-conflict", label="鉴权字段位置", ok=False,
            detail=cfg.auth_conflict + "网关两个请求头都认，所以不会直接失败，"
                   "但两处并存以后改错地方很常见，建议统一到一处。",
            fixable=FIXABLE_YES, fix_field=FIELD_AUTH, fix_value=cfg.field(FIELD_AUTH).value,
            suggested_value="只保留一处"))

    # 主字段和自定义头同时生效、内容还不一样：客户端会把这几个请求头一起发给网关，
    # 哪个头网关真正采信没有验证过，不能替用户选，只能把已知的证据摆出来。
    active: List[Tuple[str, str, str]] = []
    if has_primary:
        active.append((cfg.auth_header, rf.value.strip(), rf.source_layer or "主鉴权字段"))
    for extra in extras:
        active.append((extra.header, extra.value, extra.source))
    distinct_values = {v for _, v, _ in active}
    if len(active) > 1 and len(distinct_values) > 1:
        parts = "、".join(f"{src} 里的 {header} 头" for header, _, src in active)
        detail = (f"{parts} 同时在生效，客户端会把这几个请求头一起发给网关，"
                  "哪个头网关真正采信，这个场景没有验证过，Suture 不替你猜。")
        good = [(h, s) for h, v, s in active if gate_prefix and v.startswith(gate_prefix)]
        bad = [(h, s) for h, v, s in active if gate_prefix and not v.startswith(gate_prefix)]
        if good and bad and len(good) + len(bad) == len(active):
            good_desc = "、".join(f"{h}（{s}）" for h, s in good)
            bad_desc = "、".join(f"{h}（{s}）" for h, s in bad)
            detail += f"从 Key 前缀能看出来，{good_desc} 更像是网关签发的 Key，{bad_desc} 不是——建议保留前者，删掉后者对应的配置。"
        else:
            detail += "建议自己确认清楚以后只留一处，删掉其余的。"
        out.append(Finding(
            key="auth-multiple-active", label="鉴权信息来源冲突", ok=False, detail=detail,
            fixable=FIXABLE_NO,
            note="这几处大多是环境变量，不是配置文件里的字段，Suture 不会帮你改环境变量，需要你自己去对应的地方删掉不需要的那个。"))

    return out


def _guess_vendor(value: str, table: Dict[str, str]) -> str:
    for prefix, name in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if value.startswith(prefix):
            return name
    return ""


# ---------- 协议版本头 ----------

def check_protocol_version(cfg: HarnessConfig, profile: Dict[str, Any]) -> List[Finding]:
    rules = profile.get("protocol_version", {})
    if not rules.get("check_enabled"):
        return []
    return [Finding(
        key="protocol-version", label="协议版本头", ok=True,
        detail=f"按规则要求 {rules.get('header_name')} 不低于 {rules.get('min_version')}，"
               "客户端会自动带上这个头。")]


# ---------- 模型名称 ----------

def check_models(cfg: HarnessConfig, profile: Dict[str, Any]) -> List[Finding]:
    known = model_ids(profile)
    index = model_index(profile)
    candidates = cfg.model_candidates or (
        [cfg.field(FIELD_MODEL).value] if cfg.field(FIELD_MODEL).is_set else [])
    source_layer = cfg.field(FIELD_MODEL).source_layer

    if not candidates:
        return [Finding(
            key="model", label="模型名称", ok=False,
            detail="没有配置模型名称——这跟阶段一探活用的模型是两回事，"
                   "那个只用来验证网关活不活着，不代表你想用哪个模型，"
                   "Suture 不知道答案，不会替你选。",
            fixable=FIXABLE_NO, fix_field=FIELD_MODEL,
            choices=[{"label": m, "value": m} for m in known],
            note="从网关支持的型号里选一个")]

    out: List[Finding] = []
    for i, name in enumerate(candidates):
        out.append(_check_one_model(name, known, index, candidates, i, source_layer))
    return out


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-").replace(" ", "")


def _replaced_list(candidates: List[str], position: int, new_value: str) -> str:
    """构造「只把这一项换成 new_value，其余原样保留」之后的完整清单（逗号拼接，
    跟 apply() 已有的 value.split(",") 对应）。像 DeepSeek 这种一次能注册好几个
    模型的 harness，修一个不该把其它已经注册好的模型顶掉；单模型的 harness
    这里的 candidates 只有一项，效果等同于直接写 new_value。"""
    updated = list(candidates)
    updated[position] = new_value
    return ",".join(updated)


def _check_one_model(name: str, known: List[str], index: Dict[str, Any],
                     candidates: List[str], position: int, source_layer: str = "") -> Finding:
    label = "模型名称"
    env_hint = "；这个字段目前是被环境变量顶着生效的，修复会改写持久化的环境变量本身，" \
               "已经打开的终端/客户端需要重新打开才会生效" if source_layer == "环境变量" else ""
    if name in index:
        return Finding(key=f"model:{name}", label=label, ok=True,
                       detail=f"「{name}」在网关路由表里能精确匹配。", current_value=name)

    # 只把「看起来是打字错误」的差异当成可以自动纠正的：大小写、连字符/点号、空格
    norm = _normalize(name)
    typo_matches = [k for k in known if _normalize(k) == norm]
    if len(typo_matches) == 1:
        target = typo_matches[0]
        return Finding(
            key=f"model:{name}", label=label, ok=False,
            detail=f"「{name}」和网关登记的「{target}」只差大小写或连字符写法。",
            current_value=name, suggested_value=target,
            fixable=FIXABLE_YES, fix_field=FIELD_MODEL,
            fix_value=_replaced_list(candidates, position, target),
            note=env_hint.lstrip("；") if env_hint else "")

    # 近似匹配的结果如果本身也是一个独立型号，不能自动选——那会让用户在
    # 不知情的情况下连到另一个真实存在的模型，比直接报错更危险。
    near = difflib.get_close_matches(name, known, n=4, cutoff=0.6)
    if near:
        return Finding(
            key=f"model:{name}", label=label, ok=False,
            detail=f"「{name}」不在网关路由表里。相近的有：{'、'.join(near)}——"
                   "这些都是各自独立的型号，不是拼写变体，需要你自己确认要用哪一个，"
                   f"自动改会有连错模型的风险{env_hint}。",
            current_value=name, suggested_value="、".join(near),
            fixable=FIXABLE_NO, fix_field=FIELD_MODEL,
            choices=[{"label": c, "value": _replaced_list(candidates, position, c)}
                    for c in near])

    return Finding(
        key=f"model:{name}", label=label, ok=False,
        detail=f"「{name}」不在网关路由表里，也没有相近的名字{env_hint}。",
        current_value=name, fixable=FIXABLE_NO, fix_field=FIELD_MODEL,
        choices=[{"label": m, "value": _replaced_list(candidates, position, m)}
                for m in known],
        note=f"网关当前提供 {len(known)} 个型号，从下面选一个")


# ---------- 多层配置取值不一致 ----------

def check_layer_consistency(cfg: HarnessConfig) -> List[Finding]:
    """按已确认的使用规范，项目级/用户层不应该覆盖网关相关配置，
    出现不一致本身就算配置错误，需要统一到实际生效的值。"""
    out: List[Finding] = []
    for key in (FIELD_BASE_URL, FIELD_MODEL):
        rf = cfg.field(key)
        differing = rf.differing_layers()
        if not differing:
            continue
        others = "；".join(f"{lv.layer} 里是「{lv.value}」" for lv in differing)
        out.append(Finding(
            key=f"conflict:{key}", label="多层配置取值不一致", ok=False,
            detail=f"同一项在多个地方设置了不同的值：实际生效的是{rf.source_layer}里的"
                   f"「{rf.value}」，另外 {others}。",
            current_value=rf.value, suggested_value=rf.value,
            fixable=FIXABLE_YES, fix_field=key, fix_value=rf.value,
            note="修复会统一成实际生效的这个值，并且只改实际生效的那一层"))
    return out


# ---------- 汇总 ----------

def run_all_checks(cfg: HarnessConfig, profile: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    findings += check_config_syntax(cfg)
    findings += check_unknown_keys(cfg, profile)
    findings += check_layer_active(cfg)
    findings.append(check_base_url(cfg, profile))
    findings += check_auth(cfg, profile)
    findings += check_protocol_version(cfg, profile)
    findings += check_models(cfg, profile)
    findings += check_layer_consistency(cfg)
    return findings


def collect_info(cfg: HarnessConfig) -> List[InfoItem]:
    return [InfoItem(text=n) for n in cfg.notes]
