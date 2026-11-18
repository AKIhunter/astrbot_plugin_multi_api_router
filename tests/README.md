# 离线回归测试

## 前端表单

在插件根目录运行，需 Node.js 18 或更新版本，无需安装 npm 依赖：

```sh
node --check views/upstreams/app.js
node --test tests/test_upstream_form.cjs
```

测试使用 Node 内置 VM 执行实际页面脚本，以 DOM 替身检查提交内容，不联网。
覆盖新增 Key、替换 Key、多次编辑快照、取消路由、恢复草稿、掩码沿用、
按需揭示值沿用与替换、显式清空，以及获取模型共用的密钥沿用判定。
这些用例不等同于真实浏览器与真实服务商的端到端验证。

## Python 回归

使用已安装 AstrBot 及其依赖的 Python 环境，把插件根目录、`tests/` 和
AstrBot 源码目录加入 `PYTHONPATH`，然后运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'
```

当前包含错误分类、输入与图片预算、路由功能和保存事务回滚测试。
用例通过替身与临时文件验证逻辑，不调用真实模型；保存失败、文件权限等异常
属于预期模拟，是否通过以 unittest 最终结果为准。

如需与旧版本比较，应使用同一 Python/AstrBot 环境、相同测试入口分别运行，
按失败名称比较新增回归，保留基线已有失败的独立说明。
