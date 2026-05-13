# OpenHachimi 测试开发指南

为了保障重构和迭代质量，本项目使用 `pytest` 构建了完善的自动化测试基建。本指南将帮助你快速上手如何在本地执行和编写测试。

## 1. 安装测试依赖

在正式开始测试之前，需要安装开发与测试所需的额外依赖（如 `pytest`, `pytest-cov`, `pytest-asyncio` 等）。

请在项目根目录下执行：

```bash
# 激活你的虚拟环境（如果有）
# 安装项目自身以及测试所需依赖
pip install -e ".[dev]"
```

## 2. 启动本地测试

测试配置已经预设在 `pytest.ini` 中，只需在项目根目录运行以下命令即可：

```bash
pytest
```

执行后，终端将自动输出所有单元测试的通过状态以及简单的代码覆盖率（Coverage）概览表。

> [!TIP]
> **常用快捷命令**
> - `pytest tests/unit/test_planning.py`：仅运行指定文件的测试
> - `pytest -k "test_update_todo"`：仅运行名称匹配的特定测试用例
> - `pytest -x`：遇到第一个失败的用例立刻停止

## 3. 如何编写新的测试

所有的测试代码都应存放在 `tests/` 目录下。

### 目录结构规范
- `tests/unit/`：存放所有的单元测试文件（如 `test_deps.py`, `test_planning.py`）
- `tests/integration/`：存放集成测试
- `tests/conftest.py`：存放全局通用的 Fixtures（模拟数据与对象）

### 使用现成的 Fixtures (模拟依赖)
我们在 `tests/conftest.py` 中为你准备了一些开箱即用的模拟对象，可以直接在测试用例的参数中使用：
- `mock_config`: 一个指向临时文件夹的 `AppConfig`，防止测试污染真实用户数据。
- `mock_browser_manager`: 一个完全 Mock 掉的浏览器管理器，防止启动 Playwright 的巨大开销。
- `mock_agent_deps`: 组装好的 `AgentDeps` 依赖。

**编写示例：**

```python
import pytest

# mock_agent_deps 会被 pytest 自动注入
def test_some_feature(mock_agent_deps):
    assert mock_agent_deps.session_id == "test_session_123"
```

> [!NOTE]
> 对于协程/异步方法，你需要加上 `@pytest.mark.asyncio` 装饰器才能正确执行。

## 4. Github Actions 自动测试

项目已经集成了 Github Actions CI 流水线（配置在 `.github/workflows/pytest.yml`）。
任何针对 `main` 分支的 **Push** 或 **Pull Request** 都会在云端自动触发上述测试流程。你可以随时在 Github 仓库的 Actions 面板查看测试是否通过，从而拦截破坏性的回归 Bug。
