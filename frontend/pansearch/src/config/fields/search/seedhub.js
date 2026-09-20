export function createSeedhubGroups() {
  return [
    {
      tab: "seedhub",
      title: "SeedHub",
      icon: "mdi-seed-outline",
      fields: [
        {
          key: "seedhub_base_url",
          label: "服务地址",
          cols: 12,
        },
        {
          key: "test_seedhub",
          label: "测试搜索",
          type: "test-source",
          source: "seedhub",
          cols: 12,
        },
      ],
    },
    {
      tab: "seedhub",
      title: "搜索与风控",
      icon: "mdi-shield-search",
      hint: "搜索、详情和链接解析共享请求限速。",
      fields: [
        {
          key: "seedhub_result_limit",
          label: "候选上限",
          type: "number",
          min: 1,
          max: 80,
          cols: 4,
        },
        {
          key: "seedhub_request_interval",
          label: "请求间隔",
          type: "number",
          min: 1,
          max: 10,
          step: 0.1,
          suffix: "秒",
          cols: 4,
        },
        {
          key: "seedhub_timeout",
          label: "请求超时",
          type: "number",
          min: 5,
          max: 60,
          suffix: "秒",
          cols: 4,
        },
      ],
    },
  ]
}
