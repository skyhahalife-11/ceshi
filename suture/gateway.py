"""与网关的所有交互都在这里。

分类的关键是把「我这边连不上网」「网关自己报错」「鉴权被拒」分开：
前两类才算网关侧问题、不该去动本地配置；鉴权被拒更可能是本地配置的问题，
正是这个工具要抓的场景，不能一并甩锅给网关。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

# 判定为「网关侧问题、不碰本地配置」的分类
GATEWAY_SIDE = {"timeout", "network_error", "rate_limited", "server_error"}


@dataclass
class ProbeResult:
    ok: bool
    classification: str
    status: Optional[int]
    detail: str
    elapsed_ms: int


def _endpoint(base_url: str, suffix: str) -> str:
    return base_url.strip().rstrip("/") + suffix


ANTHROPIC_VERSION = "2023-06-01"


def _headers(auth_headers: Dict[str, str], style: str = "anthropic") -> dict:
    """auth_headers 是「请求头名字 -> 值」的完整集合，调用方已经按真实客户端
    的行为拼好了（比如 Authorization 要不要加 Bearer 前缀）——这里只管原样带上，
    不再替调用方决定该发哪一个、不发哪一个。真实客户端往往会同时发好几个
    鉴权相关的头，探活/真实请求也应该照样一起发，而不是先替用户挑一个。

    anthropic-version 是 Anthropic 协议特有的必需头，OpenAI 兼容协议里没有这个头。
    验证请求的意义在于「跟真实客户端发的一样」，所以走 OpenAI 形态的 harness
    （Codex）不该带它——带了就不是在验证用户真实会发的那种请求。"""
    headers = {"content-type": "application/json"}
    if style != "openai":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    headers.update(auth_headers or {})
    return headers


def _payload(model: str, style: str) -> dict:
    """按协议形态拼最小报文。

    两种形态在这个最小报文上恰好可以用同一组字段：`messages` 的结构一致，
    `max_tokens` 两边都认（OpenAI 新版推荐 max_completion_tokens，但兼容端点
    普遍仍接受 max_tokens，而网关背后接的是多家厂商，用兼容性最好的那个）。
    真正的差别在请求头，见 _headers()。以后如果 OpenAI 形态需要不同的报文结构，
    改这一个函数就够，不用再去动调用链。"""
    return {"model": model, "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}]}


def _classify(status: Optional[int], exc: Optional[BaseException]) -> tuple:
    if exc is not None:
        if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
            return "timeout", "请求超时，网关没有在预期时间内响应。"
        if isinstance(exc, urllib.error.URLError):
            return "network_error", f"连不上网关，本机网络或代理可能有问题：{exc.reason}"
        return "unknown", f"请求出错：{exc}"
    if status is None:
        return "unknown", "没有拿到响应状态。"
    if 200 <= status < 300:
        return "ok", "请求正常返回。"
    if status in (401, 403):
        return "auth_error", f"网关返回 {status}，鉴权被拒绝。"
    if status == 429:
        return "rate_limited", f"网关返回 {status}，被限流了。"
    if 500 <= status < 600:
        return "server_error", f"网关返回 {status}，网关侧出错。"
    return "unknown", f"网关返回了未归类的状态码 {status}。"


def _send(base_url: str, auth_headers: Dict[str, str], model: str,
          timeout: float, suffix: str = "/v1/messages", style: str = "anthropic") -> ProbeResult:
    body = json.dumps(_payload(model, style)).encode("utf-8")
    req = urllib.request.Request(_endpoint(base_url, suffix), data=body,
                                 headers=_headers(auth_headers, style), method="POST")
    started = time.time()
    status, exc = None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            e.read()
        except Exception:
            pass
    except Exception as e:      # noqa: BLE001 —— 网络异常种类多，统一交给分类函数判断
        exc = e
    elapsed = int((time.time() - started) * 1000)
    classification, detail = _classify(status, exc)
    return ProbeResult(ok=(classification == "ok"), classification=classification,
                       status=status, detail=detail, elapsed_ms=elapsed)


def probe_gateway(base_url: str, auth_headers: Dict[str, str],
                  probe_model: str, timeout: float = 8.0,
                  suffix: str = "/v1/messages", style: str = "anthropic") -> ProbeResult:
    """阶段一自检：用轻量模型确认网关这条路本身通不通。
    这个模型只用来验证网关活不活着，跟用户想用哪个模型是两件事。"""
    return _send(base_url, auth_headers, probe_model, timeout, suffix, style)


def send_real_request(base_url: str, auth_headers: Dict[str, str],
                      model: str, timeout: float = 12.0,
                      suffix: str = "/v1/messages", style: str = "anthropic") -> ProbeResult:
    """用用户实际的配置发一次最小真实请求，验证端到端能不能跑通。
    路径和报文形态必须跟这个 harness 真实会发的一致，鉴权也要把这个 harness
    真实会带上的所有请求头都带上，不只挑其中一个。"""
    return _send(base_url, auth_headers, model, timeout, suffix, style)
