export function createUpgradeSection(options) {
  return {
    value: "upgrade",
    title: "洗版设置",
    icon: "mdi-auto-fix",
    groups: [
      {
        title: "洗版功能",
        icon: "mdi-source-branch",
        hint: "候选资源继续使用优先级规则组筛选和排序；插件不附加清晰度规则。",
        fields: [
          {
            key: "enable_cloud_upgrade",
            label: "启用网盘洗版",
            type: "switch",
            hint: "从网盘候选中查找更优版本；关闭后订阅不会进入网盘洗版流程。",
            cols: 4,
          },
          {
            key: "enable_pt_upgrade",
            label: "启用 PT 洗版",
            type: "switch",
            hint: "完成 PT 下载和本地整理后，将符合洗版范围及评分条件的文件上传到当前网盘。",
            cols: 4,
          },
          {
            key: "upgrade_mode",
            label: "洗版模式",
            type: "select",
            items: [
              {title: "保留最大文件", value: "largest"},
              {title: "保留最小文件", value: "smallest"},
              {title: "直接替换", value: "replace"},
              {title: "新旧共存", value: "coexist"},
            ],
            hint: "评分更高时执行洗版；同评分时最大/最小模式按文件大小决定，默认保留最大文件。",
            cols: 4,
          },
        ],
      },
      {
        title: "洗版范围",
        icon: "mdi-movie-filter-outline",
        hint: "仅处理已开启原生 best_version 的订阅；不选择时覆盖全部此类订阅，选择后仅处理指定订阅。",
        fields: [
          {
            key: "upgrade_subscribe_ids",
            label: "单独开启洗版的订阅",
            type: "select",
            items: options.subscribes,
            multiple: true,
            searchable: true,
            hint: "所选电影或电视剧订阅参与网盘洗版。",
            cols: 12,
          },
        ],
      },
      {
        title: "洗版参数",
        icon: "mdi-tune-vertical",
        fields: [
          {
            key: "self_heal_interval",
            label: "评分自愈间隔（分钟）",
            type: "number",
            min: 0,
            hint: "按真实整理、转存和媒体库记录刷新评分；0 表示关闭。",
            cols: 4,
          },
        ],
      },
    ],
  }
}
