# MoviePilot-Plugins（wukangxxx）

个人 MoviePilot v2 插件仓库。

## 插件市场接入

在 MoviePilot 的插件市场中添加以下仓库地址：

```text
https://github.com/wukangxxx/MoviePilot-Plugins
```

插件版本、MoviePilot 最低版本及更新记录以 [package.v2.json](package.v2.json) 为准。

## 插件列表

### 115网盘订阅搜索（P115SubSearch）

115 网盘订阅搜索转存插件：

- 聚合式多渠道搜索（API 渠道 + 分享链接渠道），带 `_source` 来源标签
- 订阅缺失剧集自动搜索转存，电影全失败自动续搜下一渠道，剧集全渠道每轮必搜
- 分享链接有效性检测（支持提取/校验提取码）

### 网盘搜索助手（PanSearch）

基于 [odomu/网盘订阅助手](https://github.com/odomu/MoviePilot-Plugins) v1.3.5 二开更名，增强项：

1. **修复磁力后处理卡死**：修复 `CloudFile` 类型不匹配导致的媒体后处理无限重试，并增加连续失败 5 次熔断
2. **转存后整理开关**（默认关闭）：关闭时文件转存到中转目录即完成，不再强制移动
3. **媒体库分类归档**：按电影/电视剧分类存放（平台未返回分类目录时自动兜底）

> 注意：本插件为独立 ID（PanSearch），与上游「网盘订阅助手」互不冲突，但两者配置数据不互通，请勿同时安装。

## 仓库结构

```text
MoviePilot-Plugins/
├── frontend/       # 插件前端源码
├── icons/          # 插件图标
├── plugins.v2/     # MoviePilot v2 插件
│   ├── p115subsearch/   # 115网盘订阅搜索
│   └── pansearch/       # 网盘搜索助手（二开版）
└── package.v2.json # 插件市场清单
```
