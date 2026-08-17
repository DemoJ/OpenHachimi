"""断连自愈重试装饰器与瞬时错误判据（模块级共享，供 manager 与各 mixin 使用）。"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import wraps

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


def is_transient_disconnect(e: Exception) -> bool:
    """判断是否为断连/目标消失类瞬时错误（值得自愈重试）。

    auto_heal_retry 装饰器与各操作的内层 except 共用同一判据，
    确保断连类异常能从内层 except 中 re-raise 冒泡到装饰器触发自愈，
    而不会被吞成中文字符串导致重试逻辑变成死代码。

    注意：Playwright 的 TimeoutError 也是 PlaywrightError 子类，且消息可能
    含 "target" 等关键词，但它表示元素不可达/被遮挡（业务层面的问题），
    重启浏览器无济于事，必须显式排除，否则会误触发自愈重试浪费数十秒。
    """
    if isinstance(e, PlaywrightTimeout):
        return False
    err_str = str(e).lower()
    return (
        isinstance(e, PlaywrightError) and
        any(kw in err_str for kw in ["closed", "disconnected", "target", "protocol error", "session"])
    ) or "not open" in err_str


def auto_heal_retry(max_retries=3, base_delay=1.0):
    """
    自动恢复重试装饰器（仅对 Playwright 断连/页面崩溃类瞬时错误重试）。

    策略：
    - 只对被判定为「断连 / 会话已关闭 / 目标已消失」的错误重试，并在重试前调用
      ``_ensure_browser`` 重新获取浏览器和页面。
    - 其它异常（例如 KeyError、AttributeError、业务校验错误）一律视为代码 bug 或
      真实业务错误，不重试、立即以 ``BROWSER_OP_FAILED:`` 前缀返回，避免对真正的
      bug 反复重试浪费时间和把日志噪声放大。

    设计契约：被装饰的方法面向 LLM 工具，错误以中文字符串返回（与 BrowserManager
    内部其它方法保持一致）。重试耗尽或非瞬时错误时的失败字符串以
    ``BROWSER_OP_FAILED:`` 开头，方便监控和自动化通过前缀程序化识别失败，而不必
    依赖整句中文匹配。
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    last_err = e
                    # 检查是否为连接断开或目标关闭类型的错误（与操作内层 except 共用判据）
                    is_disconnect = is_transient_disconnect(e)

                    # 非瞬时错误：立即失败，不重试。避免对代码 bug（KeyError 等）
                    # 或业务错误反复重试，把单次失败放大成 N 倍延迟与日志噪声。
                    if not is_disconnect:
                        logger.error(
                            "Non-transient error in '%s', not retrying: %s",
                            func.__name__, e, exc_info=True,
                        )
                        return f"BROWSER_OP_FAILED: {func.__name__} 操作失败：{e}"

                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(
                            "Browser disconnected during '%s'. Attempting to heal and retry in %.1fs...",
                            func.__name__, delay, exc_info=True,
                        )
                        await asyncio.sleep(delay)
                        # 强制重置当前页面，让 _ensure_browser 重新获取或创建
                        if getattr(self, "_page", None):
                            self._page = None
                        try:
                            await self._ensure_browser()
                        except Exception as heal_err:
                            logger.error("Auto-heal failed: %s", heal_err, exc_info=True)
                    else:
                        break
            # 重试耗尽：保留 traceback 方便排查，向调用方返回带前缀的失败字符串。
            logger.error(
                "'%s' failed after %d retries. Last error: %s",
                func.__name__, max_retries, last_err, exc_info=last_err,
            )
            return f"BROWSER_OP_FAILED: {func.__name__} 操作最终失败：{last_err}"
        return wrapper
    return decorator
