"""版本号读取工具。"""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "openhachimi-agent"


def get_version() -> str:
    """获取当前已安装的版本号。

    如果包未安装（例如开发模式直接运行源码），返回 'dev'。
    """
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "dev"
