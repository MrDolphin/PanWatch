import os
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import AppSettings
from src.config import Settings
from src.core.update_checker import check_update

router = APIRouter()


def get_app_version() -> str:
    """获取应用版本号"""
    # 优先从环境变量读取
    version = os.getenv("APP_VERSION")
    if version:
        return version

    # 从 VERSION 文件读取（支持多个位置）
    possible_paths = [
        "VERSION",  # 当前工作目录（开发和生产）
        os.path.join(os.path.dirname(__file__), "../../../VERSION"),  # 相对于本文件
    ]
    for path in possible_paths:
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "dev"


class SettingUpdate(BaseModel):
    value: str


class SettingResponse(BaseModel):
    key: str
    value: str
    description: str

    class Config:
        from_attributes = True


# 配置项描述
SETTING_DESCRIPTIONS = {
    "http_proxy": "HTTP 代理地址",
    "notify_quiet_hours": "通知静默时间段（HH:MM-HH:MM，空为关闭）",
    "notify_retry_attempts": "通知失败重试次数（不含首次）",
    "notify_retry_backoff_seconds": "通知重试退避秒数（基数）",
    "notify_dedupe_ttl_overrides": "通知幂等窗口覆盖（JSON，空为默认）",
    "stock_link_platform": "股票链接平台（点击股票代码跳转的行情网站）",
    "panwatch_base_url": "PanWatch 公开访问地址（用于通知里的分析详情页链接，如 https://panwatch.example.com）",
}

SETTING_KEYS = list(SETTING_DESCRIPTIONS.keys())


def _get_env_defaults() -> dict[str, str]:
    """从 .env / 环境变量读取当前值作为默认"""
    s = Settings()
    return {
        "http_proxy": s.http_proxy,
        "notify_quiet_hours": s.notify_quiet_hours,
        "notify_retry_attempts": str(s.notify_retry_attempts),
        "notify_retry_backoff_seconds": str(s.notify_retry_backoff_seconds),
        "notify_dedupe_ttl_overrides": s.notify_dedupe_ttl_overrides,
        "stock_link_platform": "xueqiu",
        "panwatch_base_url": os.getenv("PANWATCH_BASE_URL", ""),
    }


@router.get("", response_model=list[SettingResponse])
def list_settings(db: Session = Depends(get_db)):
    settings = db.query(AppSettings).all()
    existing_map = {s.key: s for s in settings}

    env_defaults = _get_env_defaults()

    result = []
    for key in SETTING_KEYS:
        desc = SETTING_DESCRIPTIONS.get(key, "")
        env_val = env_defaults.get(key, "")

        if key not in existing_map:
            s = AppSettings(key=key, value=env_val, description=desc)
            db.add(s)
            result.append(s)
        else:
            s = existing_map[key]
            if not s.description:
                s.description = desc
            result.append(s)
    db.commit()

    return result


AVATAR_KEY = "ui_avatar"


@router.get("/avatar")
def get_avatar(db: Session = Depends(get_db)):
    """读取用户头像(data URL 或图片地址)。

    头像通过通用 PUT /settings/{AVATAR_KEY} 写入(走 catch-all,不依赖路由注册顺序),
    这里单独读取,避免大 base64 混进通用设置列表。GET /avatar 无 /{key} 同名 GET,
    不存在路由抢匹配问题。
    """
    row = db.query(AppSettings).filter(AppSettings.key == AVATAR_KEY).first()
    return {"value": (row.value if row and row.value else "")}


@router.put("/{key}", response_model=SettingResponse)
def update_setting(key: str, update: SettingUpdate, db: Session = Depends(get_db)):
    setting = db.query(AppSettings).filter(AppSettings.key == key).first()
    if not setting:
        desc = SETTING_DESCRIPTIONS.get(key, "")
        setting = AppSettings(key=key, value=update.value, description=desc)
        db.add(setting)
    else:
        setting.value = update.value

    db.commit()
    db.refresh(setting)

    # http_proxy 改动需要立刻反映到环境变量,否则 httpx 默认 Client 要重启才感知
    if key == "http_proxy":
        try:
            from server import apply_proxy_env
            apply_proxy_env(update.value)
        except Exception:
            pass

    return setting


@router.get("/version")
def get_version():
    """获取应用版本号"""
    return {"version": get_app_version()}


@router.get("/update-check")
def get_update_check(db: Session = Depends(get_db)):
    """检查是否有可用新版本（带服务端缓存）。"""
    current = get_app_version()
    app_proxy = (
        db.query(AppSettings)
        .filter(AppSettings.key == "http_proxy")
        .first()
    )
    proxy = (app_proxy.value if app_proxy and app_proxy.value else "").strip() or (
        Settings().http_proxy or ""
    )
    result = check_update(current, proxy=proxy)
    err = str(result.get("error") or "").strip()
    if err:
        return {
            "success": False,
            "code": 10061,
            "message": err,
        }
    return result
