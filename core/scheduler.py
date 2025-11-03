"""
定时任务和工具模块

本模块包含日记插件的定时任务调度器和相关工具类，负责：
1. 常量定义和配置管理
2. 模拟聊天流用于定时任务
3. 情感分析工具实现
4. 定时任务的调度和执行

主要组件：
- DiaryConstants: 插件常量定义
- MockChatStream: 虚拟聊天流类
- EmotionAnalysisTool: 情感分析工具
- DiaryScheduler: 定时任务调度器
"""

import asyncio
import datetime
import time
from typing import List, Dict, Any

from src.plugin_system import (
    BaseTool,
    ToolParamType
)
from src.plugin_system.apis import (
    get_logger
)

# 导入共享的工具类
from .utils import DiaryConstants, MockChatStream, MockMessage, ChatIdResolver
from .storage import DiaryStorage
from .actions import DiaryGeneratorAction

logger = get_logger("diary_plugin.scheduler")


class EmotionAnalysisTool(BaseTool):
    """
    情感分析工具类
    
    提供对聊天记录的情感分析功能，能够识别消息中的情感色彩，
    如开心、无语、吐槽、感动等情绪状态。支持情感分析和主题分析两种模式。
    
    该工具可以被LLM调用，用于分析聊天内容的情感倾向，为日记生成
    提供情感背景信息。
    
    Attributes:
        name (str): 工具名称
        description (str): 工具描述
        parameters (list): 工具参数定义
        available_for_llm (bool): 是否可供LLM调用
    
    Methods:
        execute: 执行情感分析
    
    Example:
        >>> tool = EmotionAnalysisTool()
        >>> result = await tool.execute({
        ...     "messages": "今天真开心，哈哈哈",
        ...     "analysis_type": "emotion"
        ... })
        >>> print(result["content"])  # "检测到的情感: 开心"
    """
    
    name = "emotion_analysis"
    description = "分析聊天记录的情感色彩,识别开心、无语、吐槽等情绪"
    parameters = [
        ("messages", ToolParamType.STRING, "聊天记录文本", True, None),
        ("analysis_type", ToolParamType.STRING, "分析类型:emotion(情感)或topic(主题)", False, ["emotion", "topic"])
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行情感分析
        
        根据输入的聊天记录文本，分析其中包含的情感色彩。
        支持两种分析模式：情感分析和主题分析。
        
        Args:
            function_args (Dict[str, Any]): 函数参数字典
                - messages (str): 要分析的聊天记录文本
                - analysis_type (str, optional): 分析类型，默认为"emotion"
        
        Returns:
            Dict[str, Any]: 分析结果字典
                - name (str): 工具名称
                - content (str): 分析结果文本
        
        Note:
            情感分析基于关键词匹配，识别以下情感：
            - 开心：哈哈、笑、开心、高兴等
            - 无语：无语、醉了、服了等  
            - 吐槽：吐槽、抱怨、烦等
            - 感动：感动、温暖、暖心等
        """
        try:
            messages = function_args.get("messages", "")
            analysis_type = function_args.get("analysis_type", "emotion")
            
            if not messages:
                return {"name": self.name, "content": "没有消息内容可分析"}
            
            if analysis_type == "emotion":
                emotions = []
                if any(word in messages for word in ["哈哈", "笑", "开心", "高兴"]):
                    emotions.append("开心")
                if any(word in messages for word in ["无语", "醉了", "服了"]):
                    emotions.append("无语")
                if any(word in messages for word in ["吐槽", "抱怨", "烦"]):
                    emotions.append("吐槽")
                if any(word in messages for word in ["感动", "温暖", "暖心"]):
                    emotions.append("感动")
                
                result = f"检测到的情感: {', '.join(emotions) if emotions else '平静'}"
            else:
                result = "聊天主题: 日常对话"
            
            return {"name": self.name, "content": result}
        except Exception as e:
            logger.error(f"情感分析失败: {e}")
            return {"name": self.name, "content": f"分析失败: {str(e)}"}


class DiaryScheduler:
    """
    日记定时任务调度器类
    
    负责管理日记插件的定时任务，包括任务的启动、停止和执行。
    根据配置的时间自动生成每日日记。
    
    该调度器支持时区配置，能够根据不同的过滤模式（白名单/黑名单）
    来决定是否启动定时任务，并在配置的时间点自动执行日记生成。
    
    Attributes:
        plugin: 插件实例引用
        is_running (bool): 任务运行状态
        task: 异步任务对象
        logger: 日志记录器
        storage: 存储管理器
    
    Methods:
        start: 启动定时任务
        stop: 停止定时任务
        _schedule_loop: 定时任务循环
        _generate_daily_diary: 生成每日日记
        _get_timezone_now: 获取配置时区的当前时间
    
    Example:
        >>> scheduler = DiaryScheduler(plugin_instance)
        >>> await scheduler.start()  # 启动定时任务
        >>> await scheduler.stop()   # 停止定时任务
    """
    
    def __init__(self, plugin):
        """
        初始化定时任务调度器
        
        Args:
            plugin: 插件实例，用于获取配置和执行日记生成
        """
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.logger = get_logger("DiaryScheduler")
        self.storage = DiaryStorage()
    
    def _get_timezone_now(self):
        """
        获取配置时区的当前时间
        
        根据插件配置中的时区设置，返回对应时区的当前时间。
        如果pytz模块未安装或时区配置错误，则回退到系统时间。
        
        Returns:
            datetime.datetime: 当前时间对象
        
        Note:
            默认时区为Asia/Shanghai，需要安装pytz模块支持时区转换
        """
        timezone_str = self.plugin.get_config("schedule.timezone", "Asia/Shanghai")
        try:
            import pytz
            tz = pytz.timezone(timezone_str)
            return datetime.datetime.now(tz)
        except ImportError:
            self.logger.error("pytz模块未安装,使用系统时间")
            return datetime.datetime.now()
        except Exception as e:
            self.logger.error(f"时区处理出错: {e},使用系统时间")
            return datetime.datetime.now()

    async def start(self):
        """
        启动定时任务
        
        检查插件配置，根据过滤模式和目标聊天列表决定是否启动定时任务。
        如果配置为白名单模式且目标列表为空，则不启动定时任务。
        
        启动成功后会创建异步任务循环，等待配置的时间点执行日记生成。
        """
        if self.is_running:
            return
        
        # 检查配置是否应该启动定时任务
        target_chats = self.plugin.get_config("schedule.target_chats", [])
        filter_mode = self.plugin.get_config("schedule.filter_mode", "whitelist")
        
        chat_resolver = ChatIdResolver()
        strategy, _ = chat_resolver.resolve_target_chats(filter_mode, target_chats)
        
        if strategy == "DISABLE_SCHEDULER":
            self.logger.info("定时任务已禁用（白名单空列表）")
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        schedule_time = self.plugin.get_config("schedule.schedule_time", "23:30")
        self.logger.info(f"定时任务已启动 - 模式: {filter_mode}, 执行时间: {schedule_time}")

    async def stop(self):
        """
        停止定时任务
        
        取消正在运行的定时任务，并等待任务完全结束。
        确保资源正确释放，避免任务泄漏。
        """
        if not self.is_running:
            return
        
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.logger.info("日记定时任务已停止")

    async def _schedule_loop(self):
        """
        定时任务循环
        
        持续运行的异步循环，计算下次执行时间并等待。
        当到达配置的时间点时，自动执行日记生成任务。
        
        循环会处理异常情况，确保单次失败不会影响后续执行。
        """
        while self.is_running:
            try:
                now = self._get_timezone_now()
                schedule_time_str = self.plugin.get_config("schedule.schedule_time", "23:30")
                
                schedule_hour, schedule_minute = map(int, schedule_time_str.split(":"))
                today_schedule = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                
                if now >= today_schedule:
                    today_schedule += datetime.timedelta(days=1)
                
                wait_seconds = (today_schedule - now).total_seconds()
                self.logger.info(f"下次日记生成时间: {today_schedule.strftime('%Y-%m-%d %H:%M:%S')}")
                
                await asyncio.sleep(wait_seconds)
                if self.is_running:
                    await self._generate_daily_diary()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"定时任务出错: {e}")
                await asyncio.sleep(60)

    async def _generate_daily_diary(self):
        """
        定时生成日记任务 - 为每个群组独立生成日记
        
        定时任务的核心执行方法，为每个配置的群组独立生成和发送日记。
        每个群组会收到基于自己群组消息的个性化日记。
        
        Note:
            根据 schedule.target_chats 配置为每个群组独立生成和发送日记
        """
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # 获取目标群组配置
            target_chats = self.plugin.get_config("schedule.target_chats", [])
            
            if not target_chats:
                self.logger.debug("未配置目标群组，跳过定时日记生成")
                return
            
            # 解析群组ID
            group_ids = []
            for chat in target_chats:
                if chat.startswith("group:"):
                    group_ids.append(chat[6:])  # 去掉 "group:" 前缀
            
            if not group_ids:
                self.logger.debug("未配置群组，跳过定时日记生成")
                return
            
            # 为每个群组独立生成日记
            success_count = 0
            for group_id in group_ids:
                try:
                    self.logger.info(f"开始为群组 {group_id} 生成日记...")
                    
                    # 为当前群组创建日记生成器
                    diary_action = DiaryGeneratorAction(
                        action_data={"date": today, "target_chats": [f"group:{group_id}"], "is_manual": False},
                        action_reasoning=f"定时生成群组 {group_id} 的日记",
                        cycle_timers={},
                        thinking_id=f"scheduled_diary_{group_id}",
                        chat_stream=MockChatStream(),
                        log_prefix=f"[ScheduledDiary-{group_id}]",
                        plugin_config=self.plugin.config,
                        action_message=MockMessage()
                    )
                    
                    # 生成当前群组的日记
                    success, result = await diary_action.generate_diary(today, target_chats=[f"group:{group_id}"])
                    
                    if success:
                        self.logger.info(f"群组 {group_id} 日记生成成功: {today} ({len(result)}字)")
                        
                        # 发送日记到当前群组
                        await self._send_diary_to_single_group(group_id, today, result)
                        success_count += 1
                    else:
                        self.logger.error(f"群组 {group_id} 日记生成失败: {today} - {result}")
                        
                except Exception as e:
                    self.logger.error(f"为群组 {group_id} 生成日记出错: {e}")
                    import traceback
                    self.logger.error(f"完整堆栈:\n{traceback.format_exc()}")
            
            if success_count > 0:
                self.logger.info(f"定时日记生成完成: 成功 {success_count}/{len(group_ids)} 个群组")
            else:
                self.logger.warning("定时日记生成失败: 所有群组均未成功")
                
        except Exception as e:
            import traceback
            self.logger.error(f"定时生成日记出错: {e}")
            self.logger.error(f"完整堆栈:\n{traceback.format_exc()}")
    
    async def _send_diary_to_single_group(self, group_id: str, date: str, diary_content: str):
        """
        发送日记到单个群组
        
        Args:
            group_id (str): 群组ID
            date (str): 日记日期
            diary_content (str): 日记内容
        """
        try:
            from src.plugin_system.apis import chat_api, send_api
            
            # 构建消息内容
            message_text = f"📅 {date} 的日记\n\n{diary_content}"
            
            # 获取群聊流
            stream = chat_api.get_stream_by_group_id(group_id)
            if not stream:
                self.logger.warning(f"无法获取群组流: {group_id}")
                return False
            
            # 使用 send_api 发送文本消息
            result = await send_api.text_to_stream(
                text=message_text,
                stream_id=stream.stream_id,
                set_reply=False,
                typing=False,
                storage_message=True
            )
            
            if result:
                self.logger.info(f"日记已发送到群组: {group_id}")
                return True
            else:
                self.logger.warning(f"发送日记到群组 {group_id} 失败: send_api 返回 False")
                return False
                
        except Exception as e:
            self.logger.error(f"发送日记到群组 {group_id} 失败: {e}")
            import traceback
            self.logger.debug(f"堆栈: {traceback.format_exc()}")
            return False
