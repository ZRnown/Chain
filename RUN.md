# 🚀 运行指南

## 快速开始

### 1. 安装依赖

```bash
# 激活虚拟环境（如果已创建）
source venv/bin/activate  # macOS/Linux
# 或
venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件（在项目根目录）：

```bash
# 必需的环境变量
TG_BOT_TOKEN=你的Telegram机器人Token
SOL_RPC_URL=https://api.mainnet-beta.solana.com
BSC_RPC_URL=https://bsc-dataseed.binance.org/

# 可选的环境变量（用于获取老鼠仓和捆绑占比）
GMGN_COOKIE=你的GMGN Cookie（可选）
GMGN_UA=你的User-Agent（可选）
GOPLUS_API_KEY=你的GoPlus API Key（可选）

# Redis（可选，用于去重）
REDIS_URL=redis://localhost:6379/0
```

**如何获取 Token：**
1. 在 Telegram 中搜索 `@BotFather`
2. 发送 `/newbot` 创建新机器人
3. 按提示设置名称和用户名
4. 获取 Token（格式类似：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`）
5. **重要**：发送 `/setprivacy` 给 BotFather，选择你的机器人，然后选择 `Disable`（关闭群组隐私模式，否则机器人无法读取群消息）

**如何获取 RPC URL：**
- **Solana**: 
  - 免费：`https://api.mainnet-beta.solana.com`
  - 或使用付费 RPC（如 QuickNode、Alchemy）
- **BSC**: 
  - 免费：`https://bsc-dataseed.binance.org/`
  - 或使用付费 RPC

### 3. 配置文件设置

```bash
# 复制示例配置
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，至少需要设置：

```yaml
telegram:
  admin_ids: [你的Telegram用户ID]  # 必须设置，用于管理命令权限
```

**如何获取你的 Telegram 用户ID：**
1. 在 Telegram 中搜索 `@userinfobot`
2. 发送任意消息给这个机器人
3. 它会返回你的用户ID（数字）

### 4. 运行机器人

```bash
# 方式1：直接运行（推荐）
python -m src.main

# 方式2：如果配置了虚拟环境
source venv/bin/activate
python -m src.main
```

### 5. 验证运行

运行后，你应该看到类似输出：

```
============================================================
🚀 CA Filter Bot Starting...
============================================================
📋 Config loaded: ca-filter-bot
💾 State store initialized
📡 DataFetcher initialized
🔄 Dedupe store initialized
============================================================
📊 Current Configuration:
   Listen chats: 0 groups
   Push chats: 0 groups
   Filters: 0 configured
============================================================
✅ Bot ready! Waiting for messages...
============================================================
```

### 6. 在 Telegram 中测试

1. 找到你的机器人（在 Telegram 中搜索你创建的用户名）
2. 发送 `/start` 测试连接
3. 发送 `/menu` 查看所有命令
4. 发送 `/settings` 查看当前配置（需要管理员权限）

## 📋 常用命令

### 管理员命令（需要 `admin_ids` 权限）

```bash
# 添加监听群组（机器人会自动监听这些群的消息）
/add_listen          # 添加当前群组
/add_listen <chat_id>  # 添加指定群组ID

# 添加推送群组（过滤后的CA会推送到这些群）
/add_push            # 添加当前群组
/add_push <chat_id>   # 添加指定群组ID

# 设置筛选条件
/set_filter market_cap_usd 5000 1000000    # 市值5K-1M
/set_filter rat_ratio null 0.15            # 老鼠仓<15%
/set_filter top10_ratio null 0.3          # 前十占比<30%

# 查看配置
/list_listen         # 查看监听群组
/list_push           # 查看推送群组
/list_filters        # 查看筛选条件
/settings            # 查看所有配置

# 设置RPC
/set_rpc solana https://api.mainnet-beta.solana.com
/set_rpc bsc https://bsc-dataseed.binance.org/
```

### 普通用户命令

```bash
/start      # 启动机器人
/menu       # 查看命令菜单
/c <CA地址>  # 手动查询合约地址
```

## 🔧 常见问题

### 1. 机器人没有反应

- ✅ 检查 Token 是否正确
- ✅ 检查是否关闭了群组隐私模式（`/setprivacy` -> `Disable`）
- ✅ 检查日志输出是否有错误

### 2. 无法读取群组消息

- ✅ 确保机器人已添加到群组
- ✅ 确保关闭了群组隐私模式
- ✅ 使用 `/add_listen` 添加群组ID到监听列表

### 3. 数据获取失败

- ✅ 检查 RPC URL 是否可访问
- ✅ 检查网络连接
- ✅ 查看日志中的详细错误信息

### 4. 图表生成失败

- ✅ 确保安装了所有依赖：`pip install -r requirements.txt`
- ✅ 检查 `/tmp` 目录是否有写入权限（Linux/Mac）

## 🐳 后台运行（生产环境）

### 使用 screen（推荐）

```bash
# 创建新的 screen 会话
screen -S ca_bot

# 运行机器人
python -m src.main

# 按 Ctrl+A 然后按 D 退出 screen（机器人继续运行）

# 重新连接
screen -r ca_bot
```

### 使用 nohup

```bash
nohup python -m src.main > bot.log 2>&1 &
```

### 使用 systemd（Linux）

创建 `/etc/systemd/system/ca-bot.service`：

```ini
[Unit]
Description=CA Filter Telegram Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/Chain
Environment="PATH=/path/to/Chain/venv/bin"
ExecStart=/path/to/Chain/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable ca-bot
sudo systemctl start ca-bot
sudo systemctl status ca-bot  # 查看状态
```

## 📝 日志

日志级别可以通过环境变量设置：

```bash
LOG_LEVEL=DEBUG python -m src.main  # 详细日志
LOG_LEVEL=INFO python -m src.main   # 普通日志（默认）
LOG_LEVEL=WARNING python -m src.main # 仅警告和错误
```

## 🎯 下一步

1. ✅ 配置监听群组和推送群组
2. ✅ 设置筛选条件
3. ✅ 测试手动查询：`/c <合约地址>`
4. ✅ 在监听群组中发送合约地址测试自动过滤

