export function createPinglianGroups(options = {}) {
  return [
    {
      tab: "pinglian",
      title: "盘链账号",
      icon: "mdi-link-variant",
      hint: "使用盘链网页登录账号搜索并解析网盘分享链接。",
      fields: [
        {
          key: "pinglian_account_info",
          type: "account",
          accountKey: "search:pinglian",
          data: options.searchAccounts?.pinglian || {},
          compact: true,
          cols: 12,
        },
        {
          key: "pinglian_base_url",
          label: "服务地址",
          cols: 12,
        },
        {
          key: "pinglian_username",
          label: "网页登录账号",
          cols: 6,
        },
        {
          key: "pinglian_password",
          label: "网页登录密码",
          type: "password",
          cols: 6,
        },
        {
          key: "test_pinglian",
          label: "测试搜索",
          type: "test-source",
          source: "pinglian",
          cols: 12,
        },
      ],
    },
    {
      tab: "pinglian",
      title: "搜索与风控",
      icon: "mdi-tune-variant",
      fields: [
        {
          key: "pinglian_result_limit",
          label: "候选上限",
          type: "number",
          min: 1,
          max: 10,
          cols: 4,
        },
        {
          key: "pinglian_request_interval",
          label: "请求访问间隔",
          type: "number",
          min: 1,
          max: 10,
          step: 0.5,
          suffix: "秒",
          cols: 4,
        },
        {
          key: "pinglian_timeout",
          label: "请求超时",
          type: "number",
          min: 5,
          max: 120,
          suffix: "秒",
          cols: 4,
        },
      ],
    },
  ]
}
