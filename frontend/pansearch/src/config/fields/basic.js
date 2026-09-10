export function createBasicSection(cloudDriveItems) {
  return {
    value: "basic",
    title: "基础设置",
    icon: "mdi-cog-outline",
    groups: [
      {
        title: "运行设置",
        icon: "mdi-play-circle-outline",
        fields: [
          {key: "enabled", label: "启用插件", type: "switch", cols: 4},
          {
            key: "show_sidebar_nav",
            label: "启用左侧导航",
            type: "switch",
            hint: "默认显示“网盘订阅”全页入口；关闭后刷新主界面生效。",
            cols: 4,
          },
          {
            key: "agent_enabled",
            label: "启用智能体工具",
            type: "switch",
            hint: "允许智能体查询状态与性能、搜索推荐资源、清理缓存、修改白名单配置，以及提交用户确认的候选。",
            cols: 4,
          },
          {
            key: "direct_transfer_enabled",
            label: "链接直达转存",
            type: "switch",
            hint: "识别普通消息中的网盘资源链接并直接进入转存流程。",
            cols: 4,
          },
          {
            key: "platform_transfer_history_enabled",
            label: "写入整理历史",
            type: "switch",
            cols: 4,
          },
          {
            key: "takeover_new_subscribes",
            label: "拦截新增订阅",
            type: "switch",
            hint: "新增订阅的搜索调度交由本插件处理，不修改原有真实站点配置。接管态仍遵循订阅过滤规则。",
            cols: 4,
          },
          {
            key: "cron",
            label: "订阅执行周期",
            type: "cron",
            hint: "设置网盘订阅自动搜索的执行周期。",
            placeholder: "30 2,10,18 * * *",
            cols: 8,
          },
        ],
      },
      {
        title: "订阅接管时段",
        icon: "mdi-clock-outline",
        hint: "接管时段内符合过滤规则的订阅由本插件搜索，时段外仍由原生搜索。平台下载策略独立决定接管时段内 PT/RSS 资源的处理方式。",
        fields: [
          {
            key: "block_system_subscribe",
            label: "始终接管系统订阅",
            hint: "订阅全天由插件接管",
            type: "switch",
            cols: 4,
          },
          {
            key: "platform_download_policy",
            label: "平台下载策略",
            type: "select",
            items: [
              {title: "允许下载并整理", value: "allow"},
              {title: "阻止搜索及下载", value: "block"},
              {title: "转为网盘离线下载", value: "cloud"},
            ],
            hint: "仅在接管态生效。阻止模式会在站点请求前终止平台搜索，并阻止插件管理订阅的下载；允许模式保留 PT/RSS 自动匹配、下载和整理，但同一季集已有网盘任务时仍会阻止重复下载；网盘模式接管失败时也不会放行平台下载。",
            cols: 8,
          },
          {
            key: "block_start_time",
            label: "接管开始",
            type: "time",
            cols: 4,
            show: (config) => !config.block_system_subscribe,
          },
          {
            key: "block_end_time",
            label: "接管结束",
            type: "time",
            cols: 4,
            show: (config) => !config.block_system_subscribe,
          },
        ],
      },
      {
        title: "网盘提供方",
        icon: "mdi-cloud-outline",
        fields: [
          {
            key: "cloud_drive",
            label: "当前转存网盘",
            type: "select",
            items: cloudDriveItems,
            disabled: () => cloudDriveItems.length <= 1,
            cols: 4,
          },
        ],
      },
    ],
  }
}
