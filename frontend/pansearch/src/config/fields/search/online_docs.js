export function createOnlineDocsGroups(resourceTypeItems) {
  return [
    {
      tab: "online_docs",
      title: "在线文档",
      icon: "mdi-file-document-outline",
      fields: [
        {
          key: "online_docs",
          label: "在线文档",
          type: "online-documents",
          items: resourceTypeItems,
          hint: "每个文档可分别选择一个或多个资源类型。",
          cols: 12,
        },
        {
          key: "test_online_docs",
          label: "测试搜索",
          type: "test-source",
          source: "online_docs",
          cols: 12,
        },
      ],
    },
  ]
}
