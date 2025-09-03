"""
日记生成核心模块

本模块包含日记生成的核心业务逻辑，是整个日记插件的核心组件。主要负责：
- 从聊天记录中提取和分析消息数据
- 根据Bot人设和聊天内容生成个性化日记
- 支持自定义模型和默认模型两种生成方式
- 处理Token限制和消息截断逻辑
- 集成QQ空间发布功能

该模块设计为独立的Action组件，可以被定时任务、手动命令等多种方式调用，
提供了完整的日记生成工作流程。

Dependencies:
    - src.plugin_system: 插件系统基础组件
    - src.plugin_system.apis: 内置API接口
    - .storage: 日记存储模块
    - .qzone: QQ空间API模块
    - .resolver: 聊天ID解析模块

Author: MaiBot Diary Plugin
Version: 2.1.0
"""

import asyncio
import datetime
import time
import random
import re
from typing import List, Tuple, Dict, Any, Optional
from openai import AsyncOpenAI

from src.plugin_system import (
    BaseAction,
    ActionActivationType
)
from src.plugin_system.apis import (
    config_api,
    llm_api,
    message_api,
    get_logger
)

from .storage import DiaryStorage, DiaryQzoneAPI, ChatIdResolver

logger = get_logger("diary_actions")

# 常量定义
class DiaryConstants:
    """日记插件常量"""
    MIN_MESSAGE_COUNT = 3
    TOKEN_LIMIT_50K = 50000
    TOKEN_LIMIT_126K = 126000
    MAX_DIARY_LENGTH = 8000
    DEFAULT_QZONE_WORD_COUNT = 300


def _format_date_str(date_input: Any) -> str:
    """
    统一的日期格式化函数,确保YYYY-MM-DD格式。
    
    支持多种日期格式的输入，包括datetime对象和多种字符串格式。
    如果所有解析方法都失败，将抛出ValueError异常。
    
    Args:
        date_input (Any): 输入的日期，可以是datetime对象或字符串
        
    Returns:
        str: 格式化后的日期字符串，格式为YYYY-MM-DD
        
    Raises:
        ValueError: 当输入的日期格式无法识别时抛出异常
        
    Examples:
        >>> _format_date_str("2025/08/24")
        "2025-08-24"
        >>> _format_date_str(datetime.datetime(2025, 8, 24))
        "2025-08-24"
    """
    if isinstance(date_input, datetime.datetime):
        return date_input.strftime("%Y-%m-%d")
    elif isinstance(date_input, str):
        try:
            # 尝试多种日期格式
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]:
                try:
                    date_obj = datetime.datetime.strptime(date_input, fmt)
                    return date_obj.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            
            # 如果已经是正确格式，直接返回
            if re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', date_input):
                return date_input
                
        except Exception as e:
            logger.debug(f"日期格式化失败: {e}")
    
    # 不再使用后备方案，而是抛出异常
    error_msg = f"无法识别的日期格式: {date_input}。支持的格式有: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD"
    logger.debug(error_msg)
    raise ValueError(error_msg)


class DiaryGeneratorAction(BaseAction):
    """
    日记生成Action - 日记插件的核心业务逻辑组件
    
    这是日记插件最重要的组件，负责完整的日记生成工作流程。主要功能包括：
    
    核心功能：
    - 智能获取和分析指定日期的聊天记录
    - 根据Bot人设生成个性化的日记内容
    - 支持情感分析和天气生成
    - 自动处理Token限制和消息截断
    - 集成QQ空间自动发布功能
    
    技术特性：
    - 支持自定义模型和系统默认模型两种生成方式
    - 智能的聊天ID解析和过滤机制
    - 完善的错误处理和重试机制
    - 灵活的配置系统支持
    
    使用场景：
    - 定时任务自动生成日记
    - 手动命令触发生成
    - 测试和调试场景
    
    配置依赖：
    - diary_generation.*: 日记生成相关配置
    - custom_model.*: 自定义模型配置
    - qzone_publishing.*: QQ空间发布配置
    - schedule.*: 定时任务配置
    
    Examples:
        # 创建日记生成器实例
        diary_action = DiaryGeneratorAction(
            action_data={"date": "2025-01-15", "target_chats": []},
            reasoning="手动生成日记",
            cycle_timers={},
            thinking_id="manual_diary",
            chat_stream=chat_stream,
            log_prefix="[DiaryGenerate]",
            plugin_config=plugin_config,
            action_message=None
        )
        
        # 执行日记生成
        success, result = await diary_action.generate_diary("2025-01-15")
        if success:
            print(f"日记生成成功: {result}")
        else:
            print(f"日记生成失败: {result}")
    
    Note:
        该类继承自BaseAction，遵循MaiBot插件系统的Action规范。
        所有的配置获取都通过self.get_config()方法进行，确保配置的一致性。
        日记生成过程中会自动保存到本地存储，并可选择发布到QQ空间。
    """
    
    action_name = "diary_generator"
    action_description = "根据当天聊天记录生成个性化日记"
    activation_type = ActionActivationType.NEVER
    
    action_parameters = {
        "date": "要生成日记的日期 (YYYY-MM-DD格式)",
        "target_chats": "目标聊天ID列表,为空则处理所有活跃聊天"
    }
    action_require = [
        "需要生成日记时使用",
        "总结当天的聊天内容",
        "生成个性化的回忆录"
    ]
    associated_types = ["text"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = DiaryStorage()
        self.qzone_api = DiaryQzoneAPI()
        self.chat_resolver = ChatIdResolver()
    
    async def get_bot_personality(self) -> Dict[str, str]:
        """
        实时获取bot人设信息
        
        从全局配置中获取Bot的人格设置，用于生成个性化的日记内容。
        适配MaiBot 0.10.2版本的新配置项结构。
        
        Returns:
            Dict[str, str]: 包含Bot人设信息的字典，包含以下键：
                - core: 核心人设描述
                - side: 情感特征/人设补充
                - style: 回复风格
                - interest: 兴趣爱好
        
        Examples:
            >>> personality = await action.get_bot_personality()
            >>> print(personality['core'])  # "是一个活泼可爱的AI助手"
            >>> print(personality['style'])  # "温和友善，偶尔调皮"
        """
        # 适配0.10.2版本的新配置项结构
        personality = config_api.get_global_config("personality.personality", "是一个机器人助手")
        reply_style = config_api.get_global_config("personality.reply_style", "")
        emotion_style = config_api.get_global_config("personality.emotion_style", "")
        interest = config_api.get_global_config("personality.interest", "")
        
        return {
            "core": personality,
            "side": emotion_style,  # 将情感特征作为人设补充
            "style": reply_style,
            "interest": interest
        }

    async def get_daily_messages(self, date: str, target_chats: List[str] = None, end_hour: int = None, end_minute: int = None) -> List[Any]:
        """
        获取指定日期的聊天记录（使用内置API）
        
        这是日记生成的核心数据获取方法，负责从消息数据库中提取指定日期的聊天记录。
        支持多种过滤模式和聊天范围配置，确保获取到合适的消息数据用于日记生成。
        
        Args:
            date (str): 目标日期，格式为YYYY-MM-DD
            target_chats (List[str], optional): 指定的聊天ID列表，为None时根据配置自动解析
            end_hour (int, optional): 结束小时，用于定时任务指定截止时间
            end_minute (int, optional): 结束分钟，用于定时任务指定截止时间
        
        Returns:
            List[Any]: 按时间排序的消息列表，每个消息包含完整的用户信息和内容
        
        Raises:
            Exception: 当消息获取过程中出现错误时
        
        Note:
            - 该方法会根据配置的过滤模式（白名单/黑名单）智能选择消息范围
            - 支持min_messages_per_chat配置，过滤掉消息数量不足的聊天
            - 所有消息都包含Bot消息（filter_mai=False），确保日记内容完整
            - 消息按时间顺序排序，便于构建时间线
        
        Examples:
            # 获取今天的所有消息
            messages = await action.get_daily_messages("2025-01-15")
            
            # 获取指定聊天的消息
            messages = await action.get_daily_messages("2025-01-15", ["chat_id_1", "chat_id_2"])
            
            # 获取到指定时间的消息（用于定时任务）
            messages = await action.get_daily_messages("2025-01-15", None, 23, 30)
        """
        try:
            # 计算时间范围
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            start_time = date_obj.timestamp()
            
            if end_hour is not None and end_minute is not None:
                end_time = date_obj.replace(hour=end_hour, minute=end_minute, second=0).timestamp()
            else:
                current_time = datetime.datetime.now()
                if current_time.strftime("%Y-%m-%d") == date:
                    end_time = current_time.timestamp()
                else:
                    end_time = (date_obj + datetime.timedelta(days=1)).timestamp()
            
            all_messages = []
            
            if target_chats:
                # 处理指定聊天
                for chat_id in target_chats:
                    try:
                        # 关键:设置 filter_mai=False 来包含Bot消息
                        messages = message_api.get_messages_by_time_in_chat(
                            chat_id=chat_id,
                            start_time=start_time,
                            end_time=end_time,
                            limit=0,
                            limit_mode="earliest",
                            filter_mai=False,  # 不过滤Bot消息
                            filter_command=False  # 不过滤命令消息
                        )
                        all_messages.extend(messages)
                    except Exception as e:
                        logger.error(f"获取聊天 {chat_id} 消息失败: {e}")
            else:
                # 从配置文件读取聊天配置
                config_target_chats = self.get_config("schedule.target_chats", [])
                filter_mode = self.get_config("schedule.filter_mode", "whitelist")
                
                # 使用新的聊天ID解析器
                strategy, resolved_chat_ids = self.chat_resolver.resolve_target_chats(filter_mode, config_target_chats)
                
                if strategy == "DISABLE_SCHEDULER":
                    # 检测到示例配置或白名单空列表的处理
                    is_manual = self.action_data.get("is_manual", False)
                    if is_manual:
                        # 手动命令:处理所有聊天（用于测试）
                        logger.debug("手动命令检测到禁用配置,处理所有聊天用于测试")
                        try:
                            messages = message_api.get_messages_by_time(
                                start_time=start_time,
                                end_time=end_time,
                                limit=0,
                                limit_mode="earliest",
                                filter_mai=False  # 不过滤Bot消息
                            )
                            all_messages.extend(messages)
                        except Exception as e:
                            logger.error(f"获取所有消息失败: {e}")
                    else:
                        # 定时任务:跳过处理,返回空消息
                        logger.debug("定时任务检测到禁用配置,取消执行")
                        return []
                
                elif strategy == "PROCESS_ALL":
                    # 黑名单空列表:处理所有聊天
                    try:
                        messages = message_api.get_messages_by_time(
                            start_time=start_time,
                            end_time=end_time,
                            limit=0,
                            limit_mode="earliest",
                            filter_mai=False  # 不过滤Bot消息
                        )
                        all_messages.extend(messages)
                    except Exception as e:
                        logger.error(f"获取所有消息失败: {e}")
                
                elif strategy == "PROCESS_WHITELIST":
                    # 白名单:只处理指定聊天
                    for chat_id in resolved_chat_ids:
                        try:
                            messages = message_api.get_messages_by_time_in_chat(
                                chat_id=chat_id,
                                start_time=start_time,
                                end_time=end_time,
                                limit=0,
                                limit_mode="earliest",
                                filter_mai=False,  # 不过滤Bot消息
                                filter_command=False  # 不过滤命令消息
                            )
                            all_messages.extend(messages)
                        except Exception as e:
                            logger.error(f"获取聊天 {chat_id} 消息失败: {e}")
                
                elif strategy == "PROCESS_BLACKLIST":
                    # 黑名单:获取所有聊天,然后排除指定聊天
                    try:
                        all_chat_messages = message_api.get_messages_by_time(
                            start_time=start_time,
                            end_time=end_time,
                            limit=0,
                            limit_mode="earliest",
                            filter_mai=False  # 不过滤Bot消息
                        )
                        
                        # 过滤掉黑名单中的聊天
                        excluded_chat_ids = set(resolved_chat_ids)
                        for msg in all_chat_messages:
                            msg_chat_id = msg.chat_id
                            if msg_chat_id not in excluded_chat_ids:
                                all_messages.append(msg)
                        
                        logger.debug(f"黑名单模式:排除了{len(excluded_chat_ids)}个聊天,处理了{len(all_messages)}条消息")
                        
                    except Exception as e:
                        logger.error(f"获取所有消息失败: {e}")
            
            # 按时间排序
            all_messages.sort(key=lambda x: x.time)
            
            # 实现min_messages_per_chat过滤逻辑
            min_messages_per_chat = self.get_config("diary_generation.min_messages_per_chat", DiaryConstants.MIN_MESSAGE_COUNT)
            if min_messages_per_chat > 0:
                # 按聊天ID分组消息
                chat_message_counts = {}
                for msg in all_messages:
                    chat_id = msg.chat_id
                    if chat_id not in chat_message_counts:
                        chat_message_counts[chat_id] = []
                    chat_message_counts[chat_id].append(msg)
                
                # 过滤出满足最少消息数量要求的聊天
                filtered_messages = []
                kept_chats = 0
                filtered_chats = 0
                
                for chat_id, messages in chat_message_counts.items():
                    if len(messages) >= min_messages_per_chat:
                        filtered_messages.extend(messages)
                        kept_chats += 1
                    else:
                        filtered_chats += 1
                
                # 重新按时间排序
                filtered_messages.sort(key=lambda x: x.time)
                logger.debug(f"消息过滤: 原始{len(all_messages)}条 → 过滤后{len(filtered_messages)}条 (min_messages_per_chat={min_messages_per_chat})")
                logger.debug(f"聊天过滤: 总聊天{len(chat_message_counts)}个 → 保留{kept_chats}个,过滤{filtered_chats}个")
                return filtered_messages
            
            return all_messages
            
        except Exception as e:
            logger.error(f"获取日期消息失败: {e}")
            return []

    def get_weather_by_emotion(self, messages: List[Any]) -> str:
        """
        根据聊天内容的情感分析生成天气
        
        通过分析聊天记录中的情感词汇，智能生成符合当天情感氛围的天气描述。
        这个功能为日记增加了情感色彩，让天气描述更贴合实际的聊天氛围。
        
        Args:
            messages (List[Any]): 消息列表，用于情感分析
        
        Returns:
            str: 生成的天气描述，如"晴"、"多云"、"雨"等
        
        Note:
            - 当enable_emotion_analysis配置为False时，随机返回中性天气
            - 情感分析基于预定义的情感词汇库
            - 天气映射规则：开心→晴天，难过→雨天，愤怒→阴天等
        
        Examples:
            >>> weather = action.get_weather_by_emotion(messages)
            >>> print(weather)  # "晴" 或 "多云" 等
        """
        enable_emotion = self.get_config("diary_generation.enable_emotion_analysis", True)
        
        if not enable_emotion or not messages:
            weather_options = ["晴", "多云", "阴", "多云转晴"]
            return random.choice(weather_options)
        
        all_content = " ".join([msg.processed_plain_text or '' for msg in messages])
        
        happy_words = ["哈哈", "笑", "开心", "高兴", "棒", "好", "赞", "爱", "喜欢"]
        sad_words = ["难过", "伤心", "哭", "痛苦", "失望"]
        angry_words = ["无语", "醉了", "服了", "烦", "气", "怒"]
        calm_words = ["平静", "安静", "淡定", "还好", "一般"]
        
        happy_count = sum(1 for word in happy_words if word in all_content)
        sad_count = sum(1 for word in sad_words if word in all_content)
        angry_count = sum(1 for word in angry_words if word in all_content)
        calm_count = sum(1 for word in calm_words if word in all_content)
        
        if happy_count >= 3:
            return "晴"
        elif happy_count >= 1:
            return "多云转晴"
        elif sad_count >= 2:
            return "雨"
        elif angry_count >= 2:
            return "阴"
        elif calm_count >= 1:
            return "多云"
        else:
            return "多云"
    
    def get_date_with_weather(self, date: str, weather: str) -> str:
        """
        生成带天气的日期字符串,兼容跨平台
        
        将日期和天气信息组合成适合日记开头的格式化字符串。
        
        Args:
            date (str): 日期字符串，格式为YYYY-MM-DD
            weather (str): 天气描述
        
        Returns:
            str: 格式化的日期天气字符串
        
        Examples:
            >>> date_weather = action.get_date_with_weather("2025-01-15", "晴")
            >>> print(date_weather)  # "2025年1月15日,星期三,晴。"
        """
        try:
            date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            weekday = weekdays[date_obj.weekday()]
            
            year = date_obj.year
            month = date_obj.month
            day = date_obj.day
            
            return f"{year}年{month}月{day}日,{weekday},{weather}。"
            
        except Exception as e:
            logger.error(f"日期格式化失败: {e}")
            return f"{date},{weather}。"

    def build_chat_timeline(self, messages: List[Any]) -> str:
        """
        构建完整对话时间线（使用内置API数据）
        
        将消息列表转换为结构化的时间线文本，用于日记生成的输入。
        按时间段分组显示消息，并区分Bot消息和用户消息。
        
        Args:
            messages (List[Any]): 按时间排序的消息列表
        
        Returns:
            str: 格式化的时间线文本，包含时间段标记和消息内容
        
        Note:
            - 消息按小时分组，显示为"上午X点"、"下午X点"、"晚上X点"
            - Bot消息显示为"我:"，用户消息显示为"昵称:"
            - 长消息会被截断为50字符并添加省略号
            - 统计信息存储在self._timeline_stats中供后续使用
        
        Examples:
            >>> timeline = action.build_chat_timeline(messages)
            >>> print(timeline)
            # 【上午9点】
            # 张三: 早上好！
            # 我: 早上好，今天天气不错呢
            # 【下午2点】
            # 李四: 下午有什么安排吗？
        """
        if not messages:
            return "今天没有什么特别的对话。"
        
        timeline_parts = []
        current_hour = -1
        bot_nickname = config_api.get_global_config("bot.nickname", "麦麦")
        bot_qq_account = str(config_api.get_global_config("bot.qq_account", ""))
        
        bot_message_count = 0
        user_message_count = 0
        
        for msg in messages:
            msg_time = datetime.datetime.fromtimestamp(msg.time)
            hour = msg_time.hour
            # 按时间段分组
            if hour != current_hour:
                if 6 <= hour < 12:
                    time_period = f"上午{hour}点"
                elif 12 <= hour < 18:
                    time_period = f"下午{hour}点"
                else:
                    time_period = f"晚上{hour}点"
                timeline_parts.append(f"\n【{time_period}】")
                current_hour = hour
            
            # 添加消息内容
            nickname = msg.user_info.user_nickname or '某人'
            user_id = str(msg.user_info.user_id)
            content = msg.processed_plain_text or ''
            if content and len(content) > 50:
                content = content[:50] + "..."
            # 判断是否为Bot消息
            if user_id == bot_qq_account:
                timeline_parts.append(f"我: {content}")
                bot_message_count += 1
            else:
                timeline_parts.append(f"{nickname}: {content}")
                user_message_count += 1
        
        # 存储统计信息
        self._timeline_stats = {
            "total_messages": len(messages),
            "bot_messages": bot_message_count,
            "user_messages": user_message_count
        }
        
        return "\n".join(timeline_parts)

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的token数量"""
        import re
        
        # 中文字符数
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        # 其他字符数
        other_chars = len(text) - chinese_chars
        # 中文约1.5字符=1token,英文约4字符=1token
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return estimated_tokens

    def estimate_token_count(self, text: str) -> int:
        """
        估算文本的token数量（公共方法）
        
        这是一个公共方法，用于外部调用估算文本的token数量。
        主要用于commands模块中的token限制检查。
        
        Args:
            text (str): 要估算的文本内容
            
        Returns:
            int: 估算的token数量
            
        Note:
            - 中文字符约1.5字符=1token
            - 英文字符约4字符=1token
            - 这是一个近似估算，实际token数可能有差异
        """
        return self._estimate_tokens(text)

    def _truncate_messages(self, timeline: str, max_tokens: int) -> str:
        """按token数量截断时间线"""
        current_tokens = self._estimate_tokens(timeline)
        
        if current_tokens <= max_tokens:
            return timeline
        
        # 按比例截断
        ratio = max_tokens / current_tokens
        target_length = int(len(timeline) * ratio * 0.95)  # 留5%余量
        
        # 智能截断,保持语句完整
        truncated = timeline[:target_length]
        
        # 找到最后一个完整句子
        for i in range(len(truncated) - 1, len(truncated) // 2, -1):  # 1为偏移量，2为半分除数
            if truncated[i] in ['。', '！', '？', '\n']:
                truncated = truncated[:i+1]
                break
        
        logger.info(f"时间线截断: {current_tokens}→{self._estimate_tokens(truncated)} tokens")
        return truncated + "\n\n[聊天记录过长,已截断]"

    def truncate_timeline_by_tokens(self, timeline: str, max_tokens: int) -> str:
        """
        按token数量截断时间线（公共方法）
        
        这是一个公共方法，用于外部调用按token数量截断时间线内容。
        主要用于commands模块中的50k token限制处理。
        
        Args:
            timeline (str): 要截断的时间线文本
            max_tokens (int): 最大token数量限制
            
        Returns:
            str: 截断后的时间线文本
            
        Note:
            - 使用智能截断，保持语句完整性
            - 会在截断处添加提示信息
            - 预留5%的token余量以确保安全
        """
        return self._truncate_messages(timeline, max_tokens)

    def smart_truncate(self, text: str, max_length: int = DiaryConstants.MAX_DIARY_LENGTH) -> str:
        """智能截断文本,保持语句完整性"""
        if len(text) <= max_length:
            return text
        
        for i in range(max_length - 3, max_length // 2, -1):  # 3为截断后缀长度，2为半分除数
            if text[i] in ['。', '！', '？', '~']:
                return text[:i+1]
        
        return text[:max_length-3] + "..."

    async def generate_with_custom_model(self, prompt: str) -> Tuple[bool, str]:
        """
        使用自定义模型生成日记
        
        调用用户配置的自定义模型API来生成日记内容。支持OpenAI格式的API接口，
        包括各种第三方服务商。提供完整的错误处理和超时控制。
        
        Args:
            prompt (str): 生成日记的提示词，包含完整的上下文信息
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的内容或错误信息)
        
        Raises:
            Exception: 当API调用失败时
        
        Note:
            - 需要配置custom_model.api_key和相关参数
            - 支持自定义超时时间和温度参数
            - 自动处理上下文长度限制
        
        Examples:
            >>> success, content = await action.generate_with_custom_model(prompt)
            >>> if success:
            >>>     print(f"生成成功: {content}")
            >>> else:
            >>>     print(f"生成失败: {content}")
        """
        try:
            from openai import AsyncOpenAI
            
            api_key = self.get_config("custom_model.api_key", "")
            if not api_key or api_key == "sk-your-siliconflow-key-here":
                return False, "自定义模型API密钥未配置"
            
            # 创建OpenAI客户端
            client = AsyncOpenAI(
                base_url=self.get_config("custom_model.api_url", "https://api.siliconflow.cn/v1"),
                api_key=api_key,
            )
            
            # 获取并验证API超时配置
            api_timeout = self.get_config("custom_model.api_timeout", 300)
            # 验证API超时是否在合理范围内（1-6000秒）
            if not (1 <= api_timeout <= 6000):
                logger.info(f"API超时配置不合理: {api_timeout}秒，将使用默认值")
                api_timeout = 300
            
            # 调用模型
            completion = await client.chat.completions.create(
                model=self.get_config("custom_model.model_name", "Pro/deepseek-ai/DeepSeek-V3"),
                messages=[{"role": "user", "content": prompt}],
                temperature=self.get_config("custom_model.temperature", 0.7),
                timeout=api_timeout
            )
            
            content = completion.choices[0].message.content
            logger.info(f"自定义模型调用成功: {self.get_config('custom_model.model_name')}")
            return True, content
            
        except Exception as e:
            logger.error(f"自定义模型调用失败: {e}")
            return False, f"自定义模型调用出错: {str(e)}"

    async def generate_with_default_model(self, prompt: str, timeline: str) -> Tuple[bool, str]:
        """
        使用默认模型生成日记（带126k截断）
        
        调用系统配置的默认模型来生成日记内容。自动处理126k token限制，
        确保输入不会超过模型的上下文长度。
        
        Args:
            prompt (str): 生成日记的提示词
            timeline (str): 时间线文本，用于token计算和截断
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的内容或错误信息)
        
        Note:
            - 强制执行126k token限制（128k-2k预留）
            - 当超过限制时自动截断时间线内容
            - 使用系统的replyer模型配置
        
        Examples:
            >>> success, content = await action.generate_with_default_model(prompt, timeline)
            >>> if success:
            >>>     print(f"生成成功: {content}")
        """
        try:
            # 默认模型强制126k截断（128k-2k预留）
            max_tokens = DiaryConstants.TOKEN_LIMIT_126K
            current_tokens = self._estimate_tokens(timeline)
            
            if current_tokens > max_tokens:
                logger.debug(f"默认模型:聊天记录超过126k tokens,进行截断")
                # 重新构建截断后的prompt
                truncated_timeline = self._truncate_messages(timeline, max_tokens)
                prompt = prompt.replace(timeline, truncated_timeline)
            
            models = llm_api.get_available_models()
            model = models.get("replyer")
            if not model:
                return False, "未找到默认模型: replyer"
            
            success, diary_content, _, _ = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model,
                request_type="plugin.diary_generation"
            )
            
            if not success or not diary_content:
                return False, "默认模型生成日记失败"
            
            return True, diary_content
            
        except Exception as e:
            logger.error(f"默认模型调用失败: {e}")
            return False, f"默认模型调用出错: {str(e)}"

    async def _publish_to_qzone(self, diary_content: str, date: str) -> bool:
        """
        发布日记到QQ空间
        
        将生成的日记内容发布到QQ空间，并更新本地存储的发布状态。
        
        Args:
            diary_content (str): 要发布的日记内容
            date (str): 日记日期
        
        Returns:
            bool: 发布是否成功
        
        Note:
            - 需要配置Napcat服务的主机和端口
            - 发布结果会更新到本地存储中
            - 失败时会记录详细的错误信息
        """
        try:
            napcat_host = self.get_config("qzone_publishing.napcat_host", "127.0.0.1")
            napcat_port = self.get_config("qzone_publishing.napcat_port", "9998")
            success = await self.qzone_api.publish_diary(diary_content, napcat_host, napcat_port)
            
            diary_data = await self.storage.get_diary(date)
            if diary_data:
                if success:
                    diary_data["is_published_qzone"] = True
                    diary_data["qzone_publish_time"] = time.time()
                    diary_data["status"] = "一切正常"
                    diary_data["error_message"] = ""
                else:
                    diary_data["is_published_qzone"] = False
                    diary_data["status"] = "报错:发说说失败"
                    diary_data["error_message"] = "原因:QQ空间发布失败,可能是cookie过期或网络问题"
                
                await self.storage.save_diary(diary_data)
            
            return success
                
        except Exception as e:
            logger.error(f"发布QQ空间失败: {e}")
            
            diary_data = await self.storage.get_diary(date)
            if diary_data:
                diary_data["is_published_qzone"] = False
                diary_data["status"] = "报错:发说说失败"
                diary_data["error_message"] = f"原因:发布异常 - {str(e)}"
                await self.storage.save_diary(diary_data)
            
            return False

    async def generate_diary(self, date: str, target_chats: List[str] = None) -> Tuple[bool, str]:
        """
        生成日记的核心逻辑（使用内置API）
        
        这是日记生成的主要入口方法，协调整个日记生成流程。包括消息获取、
        人设分析、内容生成、格式化和存储等完整步骤。
        
        Args:
            date (str): 要生成日记的日期，格式为YYYY-MM-DD
            target_chats (List[str], optional): 指定的聊天ID列表，为None时使用配置
        
        Returns:
            Tuple[bool, str]: (是否成功, 生成的日记内容或错误信息)
        
        Workflow:
            1. 获取Bot人设信息
            2. 获取指定日期的聊天消息
            3. 验证消息数量是否足够
            4. 构建对话时间线
            5. 生成情感化的天气信息
            6. 构建生成提示词
            7. 选择模型并生成内容
            8. 进行字数控制和格式化
            9. 保存到本地存储
        
        Note:
            - 支持自定义模型和默认模型两种生成方式
            - 自动处理Token限制和消息截断
            - 生成的日记会自动保存到本地JSON文件
            - 包含完整的错误处理和状态记录
        
        Examples:
            >>> success, result = await action.generate_diary("2025-01-15")
            >>> if success:
            >>>     print(f"日记生成成功: {result}")
            >>> else:
            >>>     print(f"生成失败: {result}")
        """
        try:
            # 1. 获取bot人设
            personality = await self.get_bot_personality()
            
            # 2. 获取当天消息（使用内置API）
            messages = await self.get_daily_messages(date, target_chats)
            
            if len(messages) < self.get_config("diary_generation.min_message_count", DiaryConstants.MIN_MESSAGE_COUNT):
                return False, f"当天消息数量不足({len(messages)}条),无法生成日记"
            
            # 3. 构建时间线
            timeline = self.build_chat_timeline(messages)
            
            # 4. 生成天气信息
            weather = self.get_weather_by_emotion(messages)
            date_with_weather = self.get_date_with_weather(date, weather)
            
            # 5. 生成prompt
            target_length = self.get_config("qzone_publishing.qzone_word_count", DiaryConstants.DEFAULT_QZONE_WORD_COUNT)
            
            current_time = datetime.datetime.now()
            is_today = current_time.strftime("%Y-%m-%d") == date
            time_desc = "到现在为止" if is_today else "这一天"
            
            # 构建完整的人设描述
            personality_desc = personality['core']
            if personality.get('side'):
                personality_desc += f"，{personality['side']}"
            
            # 构建兴趣描述
            interest_desc = ""
            if personality.get('interest'):
                interest_desc = f"\n我的兴趣爱好:{personality['interest']}"
            
            prompt = f"""我是{personality_desc}
我平时说话的风格是:{personality['style']}{interest_desc}

今天是{date},回顾一下{time_desc}的聊天记录:
{timeline}

现在我要写一篇{target_length}字左右的日记,记录{time_desc}的感受:
1. 开头必须是日期和天气:{date_with_weather}
2. 像睡前随手写的感觉,轻松自然
3. 回忆{time_desc}的对话,加入我的真实感受
4. 可以吐槽、感慨,体现我的个性
5. 如果有有趣的事就重点写,平淡的一天就简单记录
6. 偶尔加一两句小总结或感想
7. 不要写成流水账,要有重点和感情色彩
8. 用第一人称"我"来写
9. 结合我的兴趣爱好,对相关话题可以多写一些感想

我的日记:"""

            # 6. 根据配置选择模型生成
            use_custom_model = self.get_config("custom_model.use_custom_model", False)
            logger.debug(f"模型选择: use_custom_model={use_custom_model}")
            
            if use_custom_model:
                model_name = self.get_config("custom_model.model_name", "未知模型")
                logger.info(f"调用自定义模型: {model_name}")
                # 使用自定义模型（支持用户设置的上下文长度）
                max_context_k = self.get_config("custom_model.max_context_tokens", 256)
                # 验证上下文长度是否在合理范围内（1-10000k）
                if not (1 <= max_context_k <= 10000):
                    logger.info(f"上下文长度配置不合理: {max_context_k}k，将使用默认值")
                    max_context_k = 256
                max_context_tokens = (max_context_k * 1000) - 2000  # 自动减去2k预留
                
                current_tokens = self._estimate_tokens(timeline)
                if current_tokens > max_context_tokens:
                    logger.debug(f"自定义模型:聊天记录超过{max_context_k}k tokens,进行截断")
                    truncated_timeline = self._truncate_messages(timeline, max_context_tokens)
                    prompt = prompt.replace(timeline, truncated_timeline)
                success, diary_content = await self.generate_with_custom_model(prompt)
            else:
                logger.info("调用系统默认模型")
                # 使用默认模型（强制126k截断）
                success, diary_content = await self.generate_with_default_model(prompt, timeline)
            
            if not success or not diary_content:
                return False, diary_content or "模型生成日记失败"
            
            # 7. 字数控制
            max_length = self.get_config("qzone_publishing.qzone_word_count", DiaryConstants.DEFAULT_QZONE_WORD_COUNT)
            if max_length > DiaryConstants.MAX_DIARY_LENGTH:
                max_length = DiaryConstants.MAX_DIARY_LENGTH
            if len(diary_content) > max_length:
                diary_content = self.smart_truncate(diary_content, max_length)
            
            # 8. 保存到JSON文件（精简结构）
            diary_record = {
                "date": date,
                "diary_content": diary_content,
                "word_count": len(diary_content),
                "generation_time": time.time(),
                "weather": weather,
                "bot_messages": getattr(self, '_timeline_stats', {}).get('bot_messages', 0),
                "user_messages": getattr(self, '_timeline_stats', {}).get('user_messages', 0),
                "is_published_qzone": False,
                "qzone_publish_time": None,
                "status": "生成成功",
                "error_message": ""
            }
            
            await self.storage.save_diary(diary_record)
            
            
            return True, diary_content
            
        except Exception as e:
            logger.error(f"生成日记失败: {e}")
            try:
                failed_record = {
                    "date": date,
                    "diary_content": "",
                    "word_count": 0,
                    "generation_time": time.time(),
                    "weather": "阴",
                    "bot_messages": 0,
                    "user_messages": 0,
                    "is_published_qzone": False,
                    "qzone_publish_time": None,
                    "status": "报错:生成失败",
                    "error_message": f"原因:{str(e)}"
                }
                await self.storage.save_diary(failed_record)
            except Exception as save_error:
                logger.error(f"保存失败记录出错: {save_error}")
            
            return False, f"生成日记时出错: {str(e)}"

    async def execute(self) -> Tuple[bool, str]:
        """
        执行日记生成
        
        Action的标准执行入口，从action_data中获取参数并执行日记生成流程。
        
        Returns:
            Tuple[bool, str]: (是否成功, 执行结果描述)
        
        Note:
            - 这是BaseAction接口的实现方法
            - 会自动发送生成结果到聊天流
            - 支持手动和定时两种调用方式
        """
        date = self.action_data.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
        target_chats = self.action_data.get("target_chats", [])
        
        success, result = await self.generate_diary(date, target_chats)
        
        if success:
            await self.send_text(f"📖 {date} 的日记已生成:\n\n{result}")
            return True, f"成功生成{date}的日记"
        else:
            await self.send_text(f"❌ 日记生成失败:{result}")
            return False, result