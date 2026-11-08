# 发布整理验证记录

验证日期：2026-11-08。

本机环境：Windows、Python 3.12.14。使用独立验证虚拟环境，安装构建时复用了本机已有的构建工具；未模拟第三方包下载。

已完成：

- `pip install --no-build-isolation --no-deps -e .`：可编辑安装成功。
- `pip wheel --no-cache-dir --no-deps --no-build-isolation .`：核心 wheel 构建成功。
- `python -m unittest discover -s tests -q`：222 项通过。
- 离线命令行示例：生成成都两日行程、Markdown 和 ICS。
- 本地网页首页与健康接口响应正常；未安装 browser-use 时健康检查报告不可用。
- 网页离线 SSE 接口返回完整行程，输入预算 500 元保持为 500 元。
- 源码发布包排除 `.env`、缓存、日志、临时测试文件和个人机器路径。

本轮没有重新安装 browser-use 或执行真实网站查询。可选依赖版本按开发环境的 browser-use 0.13.14 固定；现有真实浏览器功能保留，联网可用性仍应由使用者按 README 验证。

GitHub Actions 工作流已提供，尚未在远端执行。其他操作系统尚未实机验证。原有 CSV 测试存在文件未关闭的 ResourceWarning，不影响本轮测试通过。
