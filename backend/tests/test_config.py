"""配置解析回归测试。

针对的是一个**部署阻断级**缺陷：`MGUS_ALLOWED_ORIGINS` 在文档与 compose 注释里
写的是普通 URL（`https://quiz.example.edu`），但 `allowed_origins` 是 `list[str]`，
pydantic-settings 默认按 JSON 解析 —— 于是照文档部署时 **API 在 import 期就崩**，
`SettingsError: error parsing value for field "allowed_origins"`。

修法是加 `NoDecode` 并由 validator 自己解析，同时兼容逗号分隔与 JSON 数组。
"""
import os

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("MGUS_ALLOWED_ORIGINS", None)
    yield
    os.environ.pop("MGUS_ALLOWED_ORIGINS", None)
    if old is not None:
        os.environ["MGUS_ALLOWED_ORIGINS"] = old


def test_single_url_is_accepted():
    """最自然的写法：只填一个站点来源。这是文档里给的例子，必须能用。"""
    os.environ["MGUS_ALLOWED_ORIGINS"] = "https://quiz.example.edu"
    assert Settings().allowed_origins == ["https://quiz.example.edu"]


def test_comma_separated_is_accepted():
    """多个来源用逗号分隔（docker-compose 与 .env 里的常见写法）。"""
    os.environ["MGUS_ALLOWED_ORIGINS"] = "https://quiz.example.edu,http://10.0.0.8:5173"
    assert Settings().allowed_origins == ["https://quiz.example.edu", "http://10.0.0.8:5173"]


def test_whitespace_is_trimmed():
    """现场复制粘贴很容易带空格，不能因此起不来。"""
    os.environ["MGUS_ALLOWED_ORIGINS"] = " https://a.edu , http://b.local:5173 "
    assert Settings().allowed_origins == ["https://a.edu", "http://b.local:5173"]


def test_json_array_still_works():
    """pydantic-settings 的原始要求（JSON 数组）不能被破坏。"""
    os.environ["MGUS_ALLOWED_ORIGINS"] = '["https://quiz.example.edu","http://10.0.0.8"]'
    assert Settings().allowed_origins == ["https://quiz.example.edu", "http://10.0.0.8"]


def test_empty_string_falls_back_to_empty_list_not_default():
    """显式置空 → 空列表（安全含义：不信任任何跨站来源），而不是悄悄回到默认 localhost。"""
    os.environ["MGUS_ALLOWED_ORIGINS"] = ""
    assert Settings().allowed_origins == []


def test_unset_uses_localhost_default():
    """未设置时给开发默认值，不抛错。"""
    assert Settings().allowed_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
