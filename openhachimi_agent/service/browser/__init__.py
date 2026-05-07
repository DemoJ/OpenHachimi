"""Browser management package."""

from .manager import BrowserManager
from .utils import HUMAN_VERIFICATION_REQUIRED

__all__ = ["BrowserManager", "HUMAN_VERIFICATION_REQUIRED"]
