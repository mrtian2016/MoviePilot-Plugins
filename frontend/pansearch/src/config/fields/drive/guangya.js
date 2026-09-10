export function createGuangyaGroups(options) {
  return [
    {
      tab: "guangya",
      title: "光鸭账号",
      hideHeading: true,
      fields: [
        {
          key: "guangya_account_info",
          type: "account",
          accountKey: "drive:guangya",
          data: options.accounts?.guangya || {},
          cols: 12,
        },
        {
          key: "guangya_access_token",
          label: "Access Token",
          type: "password",
          hint: "推荐点击右侧二维码按钮扫码登录",
          scanProvider: "guangya",
          cols: 6,
        },
        {
          key: "guangya_refresh_token",
          label: "Refresh Token",
          type: "password",
          cols: 6,
        },
        {
          key: "guangya_client_id",
          label: "Client ID",
          placeholder: "留空使用默认客户端",
          cols: 6,
        },
        {
          key: "guangya_device_id",
          label: "Device ID",
          placeholder: "扫码登录后自动写入",
          cols: 6,
        },
        {
          key: "guangya_transfer_path",
          label: "网盘转存路径",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "guangya",
          hint: "光鸭分享和离线任务先保存到此路径，之后按平台规则整理。",
          cols: 6,
        },
        {
          key: "guangya_media_path",
          label: "媒体库目录",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "guangya",
          hint: "最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
          cols: 6,
        },
      ],
    },
    {
      tab: "guangya",
      title: "请求超时",
      icon: "mdi-timer-cog-outline",
      hint: "作用于光鸭网盘 HTTP 请求，范围 5-300 秒。",
      fields: [
        {
          key: "guangya_request_timeout",
          label: "请求超时（秒）",
          type: "number",
          min: 5,
          max: 300,
          cols: 6,
        },
      ],
    },
  ]
}
