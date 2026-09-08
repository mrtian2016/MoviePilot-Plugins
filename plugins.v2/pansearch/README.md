# CloudSubscribe 源码目录

本目录是 `CloudSubscribe` 的 MoviePilot v2 后端源码及 Release 前端产物目录。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)。

## 目录职责

```text
cloudsubscribe/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── requirements.txt     # Python 运行依赖
├── core/                # 领域模型、配置、平台适配和通用服务
├── drive/               # 网盘能力接口及提供方实现
├── handlers/            # 搜索、订阅、同步、通知和 API 编排
├── search/              # 搜索源客户端
├── utils/               # 文件解析、匹配、Magnet 和 STRM 工具
└── dist/assets/         # CI 构建后写入 Release ZIP 的 Vue 模块联邦产物
```

## 模块边界

- `core/` 定义插件自身业务模型与服务，不放具体网盘或搜索源实现。
- `drive/` 通过稳定能力协议接入网盘；调用方先检查能力，再获取对应服务。
- `search/` 只负责获取和标准化资源候选，不执行转存或文件处理。
- `handlers/` 组合领域服务并对接事件、API、通知和任务入口。
- `utils/` 仅保留无状态或低状态的通用辅助逻辑。

## 前端与发布

前端源码位于 [`frontend/cloudsubscribe`](../../frontend/cloudsubscribe)。
[`plugins-release.yml`](../../.github/workflows/plugins-release.yml) 在发布时生成并打入插件 ZIP。

插件版本必须同时更新：

- `__init__.py` 中的 `plugin_version`
- 仓库根目录 [`package.v2.json`](../../package.v2.json) 中的 `version`
- `package.v2.json` 中对应版本的 `history`

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribe_v<version>
cloudsubscribe_v<version>.zip
```
