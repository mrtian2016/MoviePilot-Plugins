export function createJuyingGroups(options = {}) {
  return [
    {
      tab: "juying",
      title: "聚影账号",
      icon: "mdi-account-key-outline",
      hint: "使用聚影网页登录账号访问官方 WebAPI",
      fields: [
        {
          key: "juying_account_info",
          type: "account",
          accountKey: "search:juying",
          data: options.searchAccounts?.juying || {},
          compact: true,
          cols: 12,
        },
        {
          key: "juying_base_url",
          label: "服务地址",
          cols: 12,
        },
        {
          key: "juying_username",
          label: "网页登录账号",
          cols: 6,
        },
        {
          key: "juying_password",
          label: "网页登录密码",
          type: "password",
          cols: 6,
        },
        {
          key: "test_juying",
          label: "测试搜索",
          type: "test-source",
          source: "juying",
          cols: 12,
        },
      ],
    },
    {
      tab: "juying",
      title: "搜索与风控",
      icon: "mdi-shield-search",
      hint: "影片、资源和票据接口共用限速；缓存搜索结果和短时访问票据，减少重复请求。",
      fields: [
        {
          key: "juying_result_limit",
          label: "聚影候选上限",
          type: "number",
          min: 1,
          max: 20,
          cols: 6,
        },
        {
          key: "juying_request_interval",
          label: "请求访问间隔",
          hint: "登录、影片查询、资源分页和票据兑换共用统一限速，并自动加入随机抖动。",
          type: "number",
          min: 0.5,
          max: 10,
          step: 0.5,
          suffix: "秒",
          cols: 6,
        },
      ],
    },
  ]
}
