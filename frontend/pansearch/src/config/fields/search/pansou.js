export function createPansouGroups(options = {}) {
  return [
    {
      tab: "pansou",
      title: "连接配置",
      icon: "mdi-server-network",
      fields: [
        {
          key: "pansou_url",
          label: "服务地址",
          cols: 8,
        },
        {
          key: "pansou_auth_enabled",
          label: "启用身份认证",
          type: "switch",
          cols: 4,
        },
        {
          key: "pansou_username",
          label: "用户名",
          cols: 4,
          show: (config) => config.pansou_auth_enabled,
        },
        {
          key: "pansou_password",
          label: "密码",
          type: "password",
          cols: 4,
          show: (config) => config.pansou_auth_enabled,
        },
        {
          key: "test_pansou",
          label: "测试搜索",
          type: "test-source",
          source: "pansou",
          cols: 12,
        },
      ],
    },
    {
      tab: "pansou",
      title: "搜索范围",
      icon: "mdi-source-branch",
      hint:
        options.status === "ok"
          ? `服务可用：${options.channels?.length || 0} 个频道，${options.plugins?.length || 0} 个插件`
          : options.error || "保存连接配置后读取可选范围",
      fields: [
        {
          key: "pansou_channels",
          label: "限定频道",
          type: "select",
          items: options.channels || [],
          multiple: true,
          cols: 4,
        },
        {
          key: "pansou_plugins",
          label: "限定插件",
          type: "select",
          items: options.plugins || [],
          multiple: true,
          cols: 4,
        },
      ],
    },
    {
      tab: "pansou",
      title: "过滤与性能",
      icon: "mdi-filter-cog-outline",
      fields: [
        {
          key: "pansou_refresh",
          label: "强制刷新",
          hint: "开启时绕过PanSou服务端缓存；关闭可显著提高重复搜索速度",
          type: "switch",
          cols: 12,
        },
        {
          key: "pansou_filter_include",
          label: "必须包含任一关键词",
          hint: "include：结果中至少包含一个关键词（OR）",
          type: "combobox",
          cols: 6,
        },
        {
          key: "pansou_filter_exclude",
          label: "排除任一关键词",
          hint: "exclude：结果中包含任意一个关键词即排除（OR）",
          type: "combobox",
          cols: 6,
        },
        {
          key: "pansou_concurrency",
          label: "并发数（可选）",
          hint: "留空由 PanSou 按频道数和插件数自动设置",
          placeholder: "自动",
          clearable: true,
          type: "number",
          min: 1,
          max: 100,
          cols: 4,
        },
        {
          key: "pansou_result_limit",
          label: "每类结果数量",
          hint: "每种所选资源类型分别限制，默认10",
          type: "number",
          min: 1,
          max: 100,
          cols: 4,
        },
        {
          key: "pansou_timeout",
          label: "搜索超时（秒）",
          hint: "PanSou单次搜索最大等待时间，默认30秒",
          type: "number",
          min: 5,
          max: 120,
          cols: 4,
        },
      ],
    },
  ]
}
