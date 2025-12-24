#!/bin/bash

# 监控目录
WATCH_DIR="/Users/tianjy/projects/MoviePilot-Plugins-p115sub/plugins.v2/p115strgmsub"
# 目标目录
TARGET_DIR="/Users/tianjy/projects/MoviePilot/app/plugins/p115strgmsub"

# 同步函数
sync_files() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到变化，开始同步..."
    # 使用 rsync 同步，--delete 删除目标中多余的文件
    rsync -av --delete "$WATCH_DIR/" "$TARGET_DIR/"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 同步完成: $WATCH_DIR -> $TARGET_DIR"
}

# 初始同步一次
sync_files

echo "开始监控目录: $WATCH_DIR"
echo "目标目录: $TARGET_DIR"
echo "按 Ctrl+C 停止监控"
echo "-----------------------------------"

# 使用 fswatch 监控文件变化 (macOS)
if command -v fswatch &> /dev/null; then
    fswatch -o "$WATCH_DIR" | while read; do
        sync_files
    done
else
    echo "未找到 fswatch，尝试使用轮询方式..."
    echo "建议安装 fswatch: brew install fswatch"
    echo ""

    # 备用方案：轮询检测
    LAST_HASH=""
    while true; do
        CURRENT_HASH=$(find "$WATCH_DIR" -type f -exec md5 -q {} \; 2>/dev/null | sort | md5)
        if [ "$CURRENT_HASH" != "$LAST_HASH" ] && [ -n "$LAST_HASH" ]; then
            sync_files
        fi
        LAST_HASH="$CURRENT_HASH"
        sleep 2
    done
fi
