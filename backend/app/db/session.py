from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = _settings.database_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        else:
            # §17.2：连接池从 pool_size=10 / max_overflow=5 起测
            kwargs.update(pool_size=10, max_overflow=5, pool_timeout=10)
        _engine = create_async_engine(url, **kwargs)
    return _engine


def reset_engine() -> None:
    """测试辅助：更换 database_url 后重置引擎。"""
    global _engine
    _engine = None


def make_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
