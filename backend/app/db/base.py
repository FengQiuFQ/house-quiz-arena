"""SQLAlchemy 基座：声明基类、UTC 时间约定与通用列类型。

时间约定（docs/decisions.md T1）：数据库统一存 UTC。
**两种后端同构**：都是 `timestamp without time zone`，存的是"UTC 墙上时间"（naive）。
`utcnow()` 写、`as_utc()` 读，全链路不带 tzinfo，因此不存在"会话时区"这一层解释。

为什么不给 PostgreSQL 用 timestamptz
------------------------------------
本机实测（演示赛四院全 0 分）踩到的正是这个坑，且**两个方向都会错**：
asyncpg 写 `timestamptz` 时把 naive 时间按**会话时区**解释，读回来又是**会话时区**表示的
aware 时间；而 SQLite 从头到尾按文本存 naive。于是同一份代码在 SQLite 上永远正确，
到 PostgreSQL 上就出现 8 小时偏移（本机 UTC+8，且这台机器的系统时钟本身就是本地墙上
时间而时区标成 UTC，"真 UTC"与"墙上时间"只差一层谁也猜不准的解释）。
把列类型统一成 naive 之后，口径只有一个：**入库即 UTC 墙上时间**，读写对称。
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

String36 = String(36)
# 注意：**不要**改成 DateTime(timezone=True)。见模块顶部说明。
DateTimeTZ = DateTime(timezone=False)


def utcnow() -> datetime:
    """当前 UTC 时间（naive，与库内约定一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def as_utc(dt: datetime | None) -> datetime | None:
    """把库内时间读成 **UTC aware**，供比较与序列化。

    库内约定是 naive-UTC，所以这里只做"贴 UTC 标签"，**不做时区换算**。
    如果哪天读到带 tzinfo 的值（旧 timestamptz 列、外部传入），同样按
    "它的墙上时间就是 UTC 墙上时间"处理：去掉 tzinfo 再贴 UTC。
    这样两种来源在同一个数据库里也不会出现"同一时刻两种解释"。
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    """统一的对外时间格式：**UTC ISO8601 带 Z**。

    三件事都要做对：as_utc 贴标签 → 去掉微秒 → 把 +00:00 写成 Z。
    只做字符串替换是不够的：读到的若是带 +08:00 的 aware 值，那样写出来的字符串
    会被前端当成 UTC，整条时间线偏 8 小时。
    """
    d = as_utc(dt)
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z") if d else None


def server_time_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def server_time_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pk36() -> Mapped[str]:
    return mapped_column(String36, primary_key=True)
