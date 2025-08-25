# 日记插件(Diary Plugin)

让麦麦能够回忆和记录每一天的聊天，生成个性化的日记内容。

## 安装依赖

在使用插件前，请先安装必要的依赖：

```bash
cd plugins/diary_plugin
pip install -r requirements.txt
```

或手动安装：
```bash
pip install httpx pytz tomlkit openai
```

## 使用方法

### 日记命令

**注意：只有管理员才能使用所有命令**

**非管理员使用指令的响应**：
- **群聊内**：无任何响应消息（完全静默）
- **私聊内**：回复"❌ 您没有权限使用此命令。"

```bash
# 生成日记（自动发布到QQ空间）
/diary generate              # 生成今天的日记并发布到QQ空间
/diary generate 2025-08-24   # 生成指定日期的日记并发布

# 查看日记
/diary view                  # 查看今天的日记
/diary view 2025-08-24       # 查看指定日期的日记

# 日记列表
/diary list                  # 列出最近10篇日记

# 统计信息
/diary stats                 # 查看日记统计数据

# 调试功能
/diary debug                 # 调试今天的消息过滤和配置
/diary debug 2025-08-24      # 调试指定日期的消息过滤

# 帮助信息
/diary help                  # 显示所有可用命令
```

### Command指令行为说明

**白名单模式下的特殊影响**：

- **空列表时** (`target_chats = []`)：定时任务被禁用，使用指令将处理所有活跃聊天
- **设置了目标列表**：定时任务和手动命令都只处理指定聊天

**黑名单模式无特殊影响**：空列表和指定聊天的行为都很直观

## 配置说明

### 插件基础配置 [plugin]

```toml
[plugin]
enabled = true
config_version = "2.0.0"
admin_qqs = []
```

- `enabled`：控制插件是否启用
- `config_version`：配置文件版本
- `admin_qqs`：管理员QQ号列表，只有列表中的QQ号才能使用日记命令，空列表表示无人有权限

### 日记生成配置 [diary_generation]

```toml
[diary_generation]
min_message_count = 3
min_messages_per_chat = 3
enable_emotion_analysis = true
```

- `min_message_count`：生成日记所需的最少消息总数，所有聊天合计少于此值时不生成日记
- `min_messages_per_chat`：每个聊天的最少消息数量才会被处理，单个聊天消息数少于此值时跳过该聊天
- `enable_emotion_analysis`：是否根据聊天情感生成天气，关闭时随机选择天气

**消息过滤机制**：
1. 先按 `min_messages_per_chat` 过滤掉消息数量不足的单个聊天
2. 再检查剩余消息总数是否满足 `min_message_count`
3. 两个条件都满足才会生成日记

### QQ空间发布配置 [qzone_publishing]

```toml
[qzone_publishing]
qzone_word_count = 300
napcat_host = "127.0.0.1"
napcat_port = "9998"
```

- `qzone_word_count`：QQ空间说说的字数限制，范围20-8000字，超过会自动截断
- `napcat_host`：Napcat服务地址
- `napcat_port`：Napcat服务端口

**Napcat配置要求**：
1. 在napcat的webui中新建**http服务器**
2. host填**127.0.0.1**，port填**9998**
3. 启用**CORS和Websocket**

### 自定义模型配置 [custom_model]

```toml
[custom_model]
use_custom_model = false
api_url = "https://api.siliconflow.cn/v1"
api_key = "sk-your-siliconflow-key-here"
model_name = "Pro/deepseek-ai/DeepSeek-V3"
temperature = 0.7
max_context_tokens = 256
```

- `use_custom_model`：是否使用自定义模型，false时使用系统默认的首要回复模型
- `api_url`：API服务地址，必须兼容OpenAI格式，**使用基础URL格式**（不包含/chat/completions）
- `api_key`：API密钥
- `model_name`：模型名称
- `temperature`：生成温度，范围0.0-1.0，推荐0.7
- `max_context_tokens`：模型上下文长度（单位：k），填写模型真实上限

**API URL 格式说明**：
```toml
# ✅ 正确格式（基础URL）
api_url = "https://api.siliconflow.cn/v1"
api_url = "https://api.deepseek.com/v1"
api_url = "http://localhost:11434/v1"

# ❌ 错误格式（包含具体端点）
api_url = "https://api.siliconflow.cn/v1/chat/completions"
```

**重要限制**：

- 仅支持OpenAI API格式的模型服务
- 不支持Google Gemini、Anthropic Claude等原生格式
- 请确保设置的上下文长度不超过模型真实上限，否则会出现400错误

### 定时任务配置 [schedule]

```toml
[schedule]
schedule_time = "23:30"
timezone = "Asia/Shanghai"
filter_mode = "whitelist"
target_chats = []
```

- `schedule_time`：每日生成日记的时间，HH:MM格式

- `timezone`：时区设置，支持Windows/Linux/Mac等所有系统
  - 常用时区：Asia/Shanghai (中国标准时间)、Asia/Tokyo (日本标准时间)、America/New_York (美国东部时间)、America/Los_Angeles (美国西部时间)、Europe/London (英国时间)、UTC (协调世界时)

- `filter_mode`：过滤模式，可选值whitelist(白名单)或blacklist(黑名单)

- `target_chats`：目标列表，格式["group:群号", "private:用户qq号"]

**过滤模式说明**：
- **白名单模式**：只处理target_chats中指定的聊天，空列表时禁用定时任务
- **黑名单模式**：处理除target_chats外的所有聊天，空列表时处理所有聊天

**配置示例**：
```toml
# 只处理特定聊天
filter_mode = "whitelist"
target_chats = ["group:123456789", "private:987654321"]

# 排除特定聊天
filter_mode = "blacklist"
target_chats = ["group:999999"]
```

## 模型和截断机制

### 默认模型模式
- 使用系统默认的首要回复模型
- 聊天记录超过126k token时自动截断（128k-2k预留）
- 稳定可靠，成本可控

### 自定义模型模式
- 使用用户指定的自定义模型
- 根据max_context_tokens配置截断（自动减去2k预留给提示词）
- 支持更长上下文，可选择更强大的模型

## 调试功能

使用 `/diary debug` 命令可以查看：
- Bot基本信息（QQ号、昵称）
- 消息统计（完整消息、用户消息、Bot消息数量）
- 过滤配置效果（min_message_count、min_messages_per_chat）
- 过滤后可用消息数量
- 是否满足日记生成条件
- 消息示例（前5条）

这有助于理解配置对消息处理的影响，排查日记生成问题。

## 日志说明

插件会在不同日志级别显示相应信息：

### INFO级别（推荐）
- 插件启动时的配置状态
- 管理员配置情况
- 过滤模式和定时任务状态
- 模型配置（默认/自定义）
- 模型调用信息（使用的具体模型）
- 定时任务启动/禁用状态
- 日记生成和QQ空间发布结果

### DEBUG级别（详细调试）
- 配置读取详情
- Token截断过程
- 消息过滤统计
- 聊天ID映射过程
- 其他技术细节

**建议**：日常使用INFO级别即可了解插件运行状态，遇到问题时切换到DEBUG级别查看详细信息。

## 故障排除

**Q: 命令提示"权限不足"**
A: 确保QQ号已添加到admin_qqs配置中

**Q: 日记生成失败，提示"消息数量不足"**
A: 检查min_message_count和min_messages_per_chat配置，或当天确实聊天较少。使用`/diary debug`查看详细过滤情况

**Q: QQ空间发布失败**
A: 检查Napcat服务是否运行，端口配置是否正确

**Q: 自定义模型返回400错误**
A: 上下文长度超限，请降低max_context_tokens配置值

**Q: 自定义模型调用失败**
A: 检查API地址、密钥和模型名称是否正确，确认服务商支持OpenAI格式，确保api_url使用基础URL格式

**Q: 定时任务没有执行**
A: 检查filter_mode和target_chats配置，白名单空列表会禁用定时任务