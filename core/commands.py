"""
日记插件命令处理模块

本模块包含日记插件的所有命令处理逻辑，提供完整的日记管理功能。
主要功能包括日记生成、查看、列表显示、调试信息等。

Classes:
    DiaryManageCommand: 日记管理命令处理器，支持多种子命令操作

Dependencies:
    - 依赖core.storage模块进行数据存储
    - 依赖core.actions模块进行日记生成
    - 使用插件系统的消息API和配置API
    - 需要ChatIdResolver进行聊天ID解析

Author: MaiBot Team
Version: 2.1.0
"""

import asyncio
import datetime
import time
import re
from typing import List, Tuple, Dict, Any, Optional

from src.plugin_system import BaseCommand
from src.plugin_system.apis import config_api, message_api, get_logger

from .storage import DiaryStorage
from .actions import DiaryGeneratorAction

logger = get_logger("diary_commands")

# 导入必要的常量和工具类
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

class ChatIdResolver:
    """聊天ID解析器 - 将用户友好的配置转换为真实的聊天ID"""
    
    def __init__(self):
        import os
        import json
        import hashlib
        self.cache_file = os.path.join(os.path.dirname(__file__), "..", "data", "chat_mapping.json")
        self.cache = {}
        self.last_config_hash = ""
        
    def _get_config_hash(self, groups: List[str], privates: List[str]) -> str:
        """计算配置的哈希值,用于检测配置变更"""
        import hashlib
        config_str = f"groups:{','.join(sorted(groups))};privates:{','.join(sorted(privates))}"
        return hashlib.md5(config_str.encode()).hexdigest()
    
    def _load_cache(self) -> bool:
        """加载缓存文件"""
        import os
        import json
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    self.cache = cache_data.get("mapping", {})
                    self.last_config_hash = cache_data.get("config_hash", "")
                    return True
        except Exception as e:
            logger.error(f"加载聊天ID缓存失败: {e}")
        return False
    
    def _save_cache(self, config_hash: str):
        """保存缓存文件"""
        import os
        import json
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            cache_data = {
                "mapping": self.cache,
                "config_hash": config_hash,
                "last_update": time.time()
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存聊天ID缓存失败: {e}")
    
    def _query_chat_id_from_database(self, qq_number: str, is_group: bool) -> Optional[str]:
        """从数据库查询聊天ID"""
        try:
            from src.common.database.database_model import ChatStreams
            
            if is_group:
                # 查找群聊
                stream = ChatStreams.get_or_none(ChatStreams.group_id == str(qq_number))
            else:
                # 查找私聊（user_id匹配且group_id为空）
                stream = ChatStreams.get_or_none(
                    (ChatStreams.user_id == str(qq_number)) & 
                    (ChatStreams.group_id.is_null() | (ChatStreams.group_id == ""))
                )
            
            return stream.stream_id if stream else None
        except Exception as e:
            logger.error(f"查询聊天ID失败 ({qq_number}, {'群聊' if is_group else '私聊'}): {e}")
            return None
    
    def _validate_chat_id(self, chat_id: str) -> bool:
        """验证聊天ID是否有效"""
        try:
            # 尝试获取该聊天的消息来验证ID有效性
            test_messages = message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=0,
                end_time=time.time(),
                limit=1,
                filter_mai=False,
                filter_command=False
            )
            return True  # 能成功调用就说明chat_id有效
        except Exception:
            return False
    
    def resolve_filter_mode(self, filter_mode: str, target_chats: List[str], _recursion_depth: int = 0) -> Tuple[str, List[str]]:
        """根据过滤模式和目标列表解析处理策略，防止无限递归"""
        
        # 防止无限递归，最大递归深度限制为1
        if _recursion_depth > 1:
            logger.error(f"过滤模式解析递归过深，使用默认处理")
            return "PROCESS_ALL", []
        
        # 根据过滤模式处理
        if filter_mode == "whitelist":
            if target_chats:
                logger.debug(f"白名单模式:处理指定的{len(target_chats)}个聊天")
                return "PROCESS_WHITELIST", target_chats
            else:
                logger.debug("白名单模式:空列表,禁用定时任务")
                return "DISABLE_SCHEDULER", []
    
        elif filter_mode == "blacklist":
            if target_chats:
                logger.debug(f"黑名单模式:排除指定的{len(target_chats)}个聊天")
                return "PROCESS_BLACKLIST", target_chats
            else:
                logger.debug("黑名单模式:空列表,处理全部聊天")
                return "PROCESS_ALL", []
        
        else:
            logger.warning(f"未知的过滤模式: {filter_mode},使用默认白名单模式")
            return self.resolve_filter_mode("whitelist", target_chats, _recursion_depth + 1)
    
    def _parse_target_config(self, target_chats: List[str]) -> Tuple[List[str], List[str]]:
        """解析target_chats配置为群聊和私聊列表"""
        groups = []
        privates = []
        
        for chat_config in target_chats:
            if chat_config.startswith("group:"):
                group_id = chat_config[6:]  # 移除"group:"前缀
                groups.append(group_id)
            elif chat_config.startswith("private:"):
                user_id = chat_config[8:]  # 移除"private:"前缀
                privates.append(user_id)
            else:
                logger.warning(f"无效的聊天配置格式: {chat_config}")
        
        return groups, privates
    
    def resolve_target_chats(self, filter_mode: str, target_chats: List[str]) -> Tuple[str, List[str]]:
        """根据过滤模式解析目标聊天配置"""
        
        # 使用新的过滤模式解析
        strategy, valid_configs = self.resolve_filter_mode(filter_mode, target_chats)
        
        if strategy == "DISABLE_SCHEDULER":
            return strategy, []
        
        if strategy == "PROCESS_ALL":
            return strategy, []  # 空列表表示处理所有聊天
        
        if strategy in ["PROCESS_WHITELIST", "PROCESS_BLACKLIST"]:
            # 解析有效配置为聊天ID
            chat_ids = self._resolve_configs_to_chat_ids(valid_configs)
            return strategy, chat_ids
        
        return "PROCESS_ALL", []
    
    def _resolve_configs_to_chat_ids(self, target_configs: List[str]) -> List[str]:
        """将配置列表解析为聊天ID列表"""
        if not target_configs:
            return []
        
        # 解析配置
        groups, privates = self._parse_target_config(target_configs)
        
        # 计算当前配置的哈希值
        current_config_hash = self._get_config_hash(groups, privates)
        
        # 检查配置是否变更
        self._load_cache()
        config_changed = (current_config_hash != self.last_config_hash)
        
        valid_chat_ids = []
        
        # 处理群聊配置
        for group_qq in groups:
            cache_key = f"group_{group_qq}"
            
            # 优先使用缓存
            if not config_changed and cache_key in self.cache:
                cached_chat_id = self.cache[cache_key]
                if self._validate_chat_id(cached_chat_id):
                    valid_chat_ids.append(cached_chat_id)
                    continue
            
            # 缓存失效或不存在,重新查询
            chat_id = self._query_chat_id_from_database(group_qq, True)
            if chat_id and self._validate_chat_id(chat_id):
                valid_chat_ids.append(chat_id)
                self.cache[cache_key] = chat_id
                logger.debug(f"群聊映射: {group_qq} → {chat_id}")
            else:
                logger.debug(f"未找到群 {group_qq} 的聊天记录,可能尚未加入该群")
        
        # 处理私聊配置
        for user_qq in privates:
            cache_key = f"private_{user_qq}"
            
            # 优先使用缓存
            if not config_changed and cache_key in self.cache:
                cached_chat_id = self.cache[cache_key]
                if self._validate_chat_id(cached_chat_id):
                    valid_chat_ids.append(cached_chat_id)
                    continue
            
            # 缓存失效或不存在,重新查询
            chat_id = self._query_chat_id_from_database(user_qq, False)
            if chat_id and self._validate_chat_id(chat_id):
                valid_chat_ids.append(chat_id)
                self.cache[cache_key] = chat_id
                logger.debug(f"私聊映射: {user_qq} → {chat_id}")
            else:
                logger.debug(f"未找到用户 {user_qq} 的聊天记录,可能尚未建立私聊")
        
        # 保存更新后的缓存
        if config_changed or valid_chat_ids:
            self._save_cache(current_config_hash)
        
        logger.debug(f"聊天ID解析完成: 配置{len(groups + privates)}个,有效{len(valid_chat_ids)}个")
        return valid_chat_ids

class MockChatStream:
    """虚拟聊天流,用于定时任务中的Action初始化"""
    
    def __init__(self):
        self.stream_id = "diary_scheduled_task"
        self.platform = "qq"
        self.group_info = None
        self.user_info = None

class DiaryManageCommand(BaseCommand):
    """
    日记管理命令处理器
    
    这是日记插件的核心命令处理模块，负责处理所有与日记相关的用户命令。
    支持多种子命令操作，包括日记生成、查看、列表显示、调试信息等功能。
    
    支持的命令:
        /diary generate [日期] - 手动生成指定日期的日记（默认今天）
        /diary list [参数] - 查看日记列表和统计信息
        /diary view [日期] [编号] - 查看指定日记内容
        /diary debug [日期] - 显示系统调试信息（默认今天）
        /diary help - 显示帮助信息
    
    权限控制:
        - generate, list, debug, help: 仅管理员可用
        - view: 所有用户可用
    
    环境检测:
        - 群聊环境: 只处理当前群的消息
        - 私聊环境: 处理全局消息
    
    特性:
        - 智能环境检测和消息获取
        - 完整的错误处理和用户反馈
        - 详细的调试信息和统计数据
        - 支持多种日期格式输入
        - 权限分级管理
    
    Attributes:
        command_name (str): 命令名称 "diary"
        command_description (str): 命令描述
        command_pattern (str): 命令匹配正则表达式
        storage (DiaryStorage): 日记存储管理器
    
    Methods:
        execute(): 命令执行入口，根据子命令分发处理
        _get_messages_with_context_detection(): 智能消息获取和环境检测
        _analyze_user_activity(): 用户活跃度分析
        _get_date_message_stats(): 日期消息统计
        _build_debug_info(): 构建调试信息文本
        _show_specific_diary(): 显示指定编号的日记内容
        _show_diary_list(): 显示日记列表
        _generate_diary_with_50k_limit(): 使用50k限制生成日记
        _get_next_schedule_time(): 计算下次定时任务时间
        _get_weekly_stats(): 计算本周统计数据
    
    Examples:
        >>> # 生成今天的日记
        >>> /diary generate
        
        >>> # 生成指定日期的日记
        >>> /diary generate 2025-01-15
        
        >>> # 查看日记概览
        >>> /diary list
        
        >>> # 查看指定日期的日记列表
        >>> /diary list 2025-01-15
        
        >>> # 查看详细统计
        >>> /diary list all
        
        >>> # 查看今天的日记
        >>> /diary view
        
        >>> # 查看指定日期的第2条日记
        >>> /diary view 2025-01-15 2
        
        >>> # 显示调试信息
        >>> /diary debug 2025-01-15
    
    Note:
        该类包含了所有之前修复的问题和优化，确保稳定运行。
        所有方法都包含完整的错误处理和日志记录。
    """
    
    command_name = "diary"
    command_description = "日记管理命令集合"
    command_pattern = r"^/diary\s+(?P<action>list|generate|help|debug|view)(?:\s+(?P<param>.+))?\s*$"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = DiaryStorage()
    
    async def _get_next_schedule_time(self) -> str:
        """
        计算下次定时任务时间
        
        根据配置的定时任务时间和时区，计算下一次日记生成的具体时间。
        如果当前时间已经超过今天的定时时间，则计算明天的定时时间。
        
        Returns:
            str: 下次定时任务的时间字符串，格式为 'YYYY-MM-DD HH:MM'
                如果计算失败则返回 "计算失败"
        
        Note:
            - 支持时区配置，默认使用 Asia/Shanghai
            - 需要 pytz 模块支持，如果未安装则使用系统时间
            - 定时时间格式为 HH:MM，默认为 23:30
        """
        try:
            schedule_time = self.get_config("schedule.schedule_time", "23:30")
            timezone_str = self.get_config("schedule.timezone", "Asia/Shanghai")
            
            try:
                import pytz
                tz = pytz.timezone(timezone_str)
                now = datetime.datetime.now(tz)
            except ImportError:
                now = datetime.datetime.now()
            
            schedule_hour, schedule_minute = map(int, schedule_time.split(":"))
            today_schedule = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
            
            if now >= today_schedule:
                today_schedule += datetime.timedelta(days=1)
            
            return today_schedule.strftime('%Y-%m-%d %H:%M')
        except Exception as e:
            logger.error(f"计算下次定时任务时间失败: {e}")
            return "计算失败"
    
    async def _get_weekly_stats(self, diaries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        计算本周统计数据
        
        分析本周和上周的日记数据，计算各种统计指标和趋势变化。
        
        Args:
            diaries (List[Dict[str, Any]]): 所有日记数据列表
        
        Returns:
            Dict[str, Any]: 包含本周统计数据的字典，包含以下字段：
                - total_count (int): 本周日记总数
                - avg_words (int): 本周平均字数
                - success_count (int): 本周成功发布数
                - success_rate (float): 本周发布成功率
                - trend (str): 与上周对比的趋势描述
        
        Note:
            - 本周定义为从周一开始到当前时间
            - 趋势对比基于平均字数的变化
            - 如果计算失败，返回默认的零值数据
        """
        try:
            now = datetime.datetime.now()
            # 计算本周开始时间（周一）
            days_since_monday = now.weekday()
            week_start = now - datetime.timedelta(days=days_since_monday)
            week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
            
            # 计算上周开始时间
            last_week_start = week_start - datetime.timedelta(days=7)
            
            # 过滤本周和上周的日记
            this_week_diaries = []
            last_week_diaries = []
            
            for diary in diaries:
                diary_time = datetime.datetime.fromtimestamp(diary.get('generation_time', 0))
                if diary_time >= week_start:
                    this_week_diaries.append(diary)
                elif diary_time >= last_week_start and diary_time < week_start:
                    last_week_diaries.append(diary)
            
            # 计算本周统计
            this_week_count = len(this_week_diaries)
            this_week_words = sum(diary.get("word_count", 0) for diary in this_week_diaries)
            this_week_avg = this_week_words // this_week_count if this_week_count > 0 else 0
            this_week_success = sum(1 for diary in this_week_diaries if diary.get("is_published_qzone", False))
            this_week_success_rate = (this_week_success / this_week_count * 100) if this_week_count > 0 else 0
            
            # 计算上周统计
            last_week_count = len(last_week_diaries)
            last_week_words = sum(diary.get("word_count", 0) for diary in last_week_diaries)
            last_week_avg = last_week_words // last_week_count if last_week_count > 0 else 0
            
            # 计算趋势
            if last_week_avg > 0:
                word_diff = this_week_avg - last_week_avg
                if word_diff > 0:
                    trend = f"↑ 比上周多{word_diff}字"
                elif word_diff < 0:
                    trend = f"↓ 比上周少{abs(word_diff)}字"
                else:
                    trend = "→ 与上周持平"
            else:
                trend = "📈 本周新数据"
            
            return {
                "total_count": this_week_count,
                "avg_words": this_week_avg,
                "success_count": this_week_success,
                "success_rate": this_week_success_rate,
                "trend": trend
            }
        except Exception as e:
            logger.error(f"计算本周统计失败: {e}")
            return {
                "total_count": 0,
                "avg_words": 0,
                "success_count": 0,
                "success_rate": 0,
                "trend": "计算失败"
            }

    def _calculate_end_time(self, date_obj: datetime.datetime, date: str) -> float:
        """
        计算结束时间
        
        根据指定日期计算消息查询的结束时间戳。如果是今天，则使用当前时间；
        如果是历史日期，则使用该日期的23:59:59。
        
        Args:
            date_obj (datetime.datetime): 日期对象
            date (str): 日期字符串，格式为 YYYY-MM-DD
        
        Returns:
            float: 结束时间的时间戳
        """
        current_time = datetime.datetime.now()
        if current_time.strftime("%Y-%m-%d") == date:
            return current_time.timestamp()
        else:
            return (date_obj + datetime.timedelta(days=1)).timestamp()

    async def _get_messages_with_context_detection(self, date: str) -> Tuple[List[Any], str]:
        """
        根据命令环境智能获取消息
        
        这是一个核心方法，负责根据命令执行环境（群聊/私聊）智能获取相应的消息数据。
        包含完整的错误处理和环境检测逻辑。
        
        Args:
            date (str): 要查询的日期，格式为 YYYY-MM-DD
        
        Returns:
            Tuple[List[Any], str]: 返回消息列表和环境描述
                - List[Any]: 获取到的消息列表
                - str: 环境描述字符串，用于日志和用户反馈
        
        Raises:
            ValueError: 当日期格式无效时抛出
            Exception: 当消息获取过程中出现其他错误时抛出
        
        Note:
            - 群聊环境：只获取当前群的消息
            - 私聊环境：获取全局消息
            - 包含完整的错误处理和诊断信息
            - 支持数据验证和质量检查
        """
        error_context = ""
        try:
            # 数据验证
            if not date or not isinstance(date, str):
                raise ValueError(f"无效的日期参数: {date}")
            
            error_context = "时间计算阶段"
            # 计算时间范围
            try:
                date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
                start_time = date_obj.timestamp()
                end_time = self._calculate_end_time(date_obj, date)
                logger.debug(f"[DEBUG] 时间范围: {date} ({start_time} - {end_time})")
            except ValueError as date_error:
                raise ValueError(f"日期格式错误: {date}, 错误: {date_error}")
            
            error_context = "环境检测阶段"
            # 检测命令环境
            try:
                group_info = self.message.message_info.group_info if hasattr(self.message, 'message_info') else None
            except Exception as env_error:
                logger.warning(f"[DEBUG] 环境检测失败: {env_error}")
                group_info = None
            
            if group_info:
                error_context = "群聊消息获取阶段"
                # 群聊环境：只处理当前群
                try:
                    group_id = str(group_info.group_id) if group_info.group_id else ""
                    if not group_id:
                        raise ValueError("群号为空")
                    
                    logger.debug(f"[DEBUG] 群聊模式: 群号 {group_id}")
                    
                    # 查询群号对应的stream_id
                    chat_resolver = ChatIdResolver()
                    stream_id = chat_resolver._query_chat_id_from_database(group_id, True)
                    
                    if stream_id:
                        try:
                            messages = message_api.get_messages_by_time_in_chat(
                                chat_id=stream_id,
                                start_time=start_time,
                                end_time=end_time,
                                limit=0,
                                limit_mode="earliest",
                                filter_mai=False,
                                filter_command=False
                            )
                            context_desc = f"【本群】({group_id}→{stream_id})"
                            logger.info(f"[DEBUG] 群聊模式成功: 群号 {group_id} → stream_id {stream_id}, 获取{len(messages)}条消息")
                        except Exception as api_error:
                            logger.error(f"[DEBUG] 消息API调用失败: {api_error}")
                            messages = []
                            context_desc = f"【本群】({group_id}→API失败)"
                    else:
                        messages = []
                        context_desc = f"【本群】({group_id}→未找到)"
                        # 增强错误处理和诊断信息
                        logger.error(f"[DEBUG] stream_id查询失败: 群号 {group_id} 在数据库中未找到对应的聊天记录")
                        logger.error(f"[DEBUG] 可能的原因:")
                        logger.error(f"[DEBUG] 1. 该群聊尚未有任何消息记录")
                        logger.error(f"[DEBUG] 2. 群号配置错误或群聊已解散")
                        logger.error(f"[DEBUG] 3. 数据库中ChatStreams表缺少该群的记录")
                        logger.error(f"[DEBUG] 4. 群号格式问题: 当前群号='{group_id}' (类型: {type(group_id).__name__})")
                        logger.error(f"[DEBUG] 建议解决方案:")
                        logger.error(f"[DEBUG] - 检查群号是否正确: {group_id}")
                        logger.error(f"[DEBUG] - 确认Bot已加入该群并有消息交互")
                        logger.error(f"[DEBUG] - 检查数据库ChatStreams表中是否存在group_id='{group_id}'的记录")
                        
                except Exception as group_error:
                    logger.error(f"[DEBUG] 群聊处理失败: {group_error}")
                    messages = []
                    context_desc = f"【本群】(处理失败)"
            else:
                error_context = "全局消息获取阶段"
                # 私聊环境：处理所有消息
                try:
                    messages = message_api.get_messages_by_time(
                        start_time=start_time,
                        end_time=end_time,
                        filter_mai=False
                    )
                    context_desc = "【全局日记】"
                    logger.info(f"[DEBUG] 私聊模式成功: 获取{len(messages)}条全局消息")
                except Exception as global_error:
                    logger.error(f"[DEBUG] 全局消息获取失败: {global_error}")
                    messages = []
                    context_desc = "【全局】(获取失败)"
            
            # 验证返回数据
            if not isinstance(messages, list):
                logger.warning(f"[DEBUG] 消息API返回了非列表类型: {type(messages)}")
                messages = []
            
            logger.info(f"[DEBUG] 消息获取完成: {context_desc}, 共{len(messages)}条消息")
            return messages, context_desc
            
        except ValueError as ve:
            logger.error(f"[DEBUG] 参数验证失败 ({error_context}): {ve}")
            return [], f"【参数错误】"
        except Exception as e:
            logger.error(f"[DEBUG] 消息获取失败 ({error_context}): {e}")
            logger.error(f"[DEBUG] 错误详情: 日期={date}, 阶段={error_context}")
            return [], f"【{error_context}失败】"

    def _analyze_user_activity(self, messages: List[Any], bot_qq: str) -> List[Dict[str, Any]]:
        """
        分析用户活跃度
        
        分析消息列表中各用户的活跃程度，统计消息数量并识别Bot消息。
        包含完整的数据验证和错误处理。
        
        Args:
            messages (List[Any]): 消息列表
            bot_qq (str): Bot的QQ号，用于识别Bot消息
        
        Returns:
            List[Dict[str, Any]]: 用户活跃度统计列表，按消息数量降序排列
                每个元素包含：
                - user_id (str): 用户ID
                - nickname (str): 用户昵称
                - message_count (int): 消息数量
                - is_identified_as_bot (bool): 是否识别为Bot
        
        Note:
            - 返回前10个最活跃用户
            - 包含数据质量检查和错误统计
            - 安全处理各种异常情况
        """
        try:
            # 数据验证
            if not isinstance(messages, list):
                logger.warning(f"[DEBUG] 用户活跃度分析: 消息参数不是列表类型: {type(messages)}")
                return []
            
            if not bot_qq or not isinstance(bot_qq, str):
                logger.warning(f"[DEBUG] 用户活跃度分析: Bot QQ参数无效: {bot_qq}")
                bot_qq = ""
            
            user_stats = {}
            processed_count = 0
            error_count = 0
            
            for i, msg in enumerate(messages):
                try:
                    # 验证消息结构
                    if not hasattr(msg, 'user_info') or not msg.user_info:
                        logger.debug(f"[DEBUG] 消息{i}缺少user_info，跳过")
                        error_count += 1
                        continue
                    
                    # 安全获取用户ID
                    try:
                        user_id = str(msg.user_info.user_id) if msg.user_info.user_id is not None else "unknown"
                    except Exception as uid_error:
                        logger.debug(f"[DEBUG] 获取用户ID失败(消息{i}): {uid_error}")
                        user_id = "unknown"
                        error_count += 1
                    
                    # 安全获取昵称
                    try:
                        nickname = msg.user_info.user_nickname if msg.user_info.user_nickname else '未知用户'
                        # 确保昵称是字符串
                        nickname = str(nickname) if nickname else '未知用户'
                    except Exception as nick_error:
                        logger.debug(f"[DEBUG] 获取昵称失败(消息{i}): {nick_error}")
                        nickname = '未知用户'
                    
                    # 创建统计条目
                    key = (user_id, nickname)
                    if key not in user_stats:
                        user_stats[key] = {
                            'user_id': user_id,
                            'nickname': nickname,
                            'message_count': 0,
                            'is_identified_as_bot': user_id == bot_qq
                        }
                    user_stats[key]['message_count'] += 1
                    processed_count += 1
                    
                except Exception as msg_error:
                    logger.debug(f"[DEBUG] 处理消息{i}时出错: {msg_error}")
                    error_count += 1
                    continue
            
            # 转换为列表并排序
            try:
                stats_list = list(user_stats.values())
                stats_list.sort(key=lambda x: x.get('message_count', 0), reverse=True)
                result = stats_list[:10]  # 返回前10个活跃用户
                
                logger.info(f"[DEBUG] 用户活跃度分析完成: 处理{processed_count}条消息, 错误{error_count}条, 用户{len(user_stats)}个, 返回{len(result)}个")
                
                # 数据质量检查
                if error_count > processed_count * 0.1:  # 错误率超过10%
                    logger.warning(f"[DEBUG] 用户活跃度分析数据质量较差: 错误率{error_count}/{processed_count + error_count}")
                
                return result
                
            except Exception as sort_error:
                logger.error(f"[DEBUG] 用户统计排序失败: {sort_error}")
                # 返回未排序的结果
                return list(user_stats.values())[:10]
            
        except Exception as e:
            logger.error(f"[DEBUG] 用户活跃度分析失败: {e}")
            logger.error(f"[DEBUG] 分析参数: 消息数量={len(messages) if isinstance(messages, list) else 'N/A'}, Bot QQ={bot_qq}")
            return []

    async def _get_date_message_stats(self, date: str, bot_qq: str) -> Dict[str, Any]:
        """
        获取指定日期的消息统计
        
        获取指定日期的详细消息统计信息，包括总消息数、Bot消息数、用户消息数、
        活跃聊天数等。包含完整的错误处理和数据质量检查。
        
        Args:
            date (str): 要统计的日期，格式为 YYYY-MM-DD
            bot_qq (str): Bot的QQ号，用于区分Bot消息和用户消息
        
        Returns:
            Dict[str, Any]: 消息统计信息字典，包含以下字段：
                - total_messages (int): 总消息数
                - bot_messages (int): Bot消息数
                - user_messages (int): 用户消息数
                - active_chats (int): 活跃聊天数
                - context_desc (str): 环境描述
                - valid_messages (int): 有效消息数
                - data_quality (str): 数据质量评估
                - error_detail (str): 错误详情（如果有错误）
        
        Note:
            - 包含数据一致性检查
            - 提供详细的错误诊断信息
            - 支持部分数据处理
        """
        error_context = ""
        try:
            # 数据验证
            if not date or not isinstance(date, str):
                raise ValueError(f"无效的日期参数: {date}")
            if not bot_qq or not isinstance(bot_qq, str):
                raise ValueError(f"无效的Bot QQ参数: {bot_qq}")
            
            error_context = "消息获取阶段"
            messages, context_desc = await self._get_messages_with_context_detection(date)
            
            # 验证消息数据
            if not isinstance(messages, list):
                logger.warning(f"[DEBUG] 消息获取返回了非列表类型: {type(messages)}")
                messages = []
            
            error_context = "消息统计阶段"
            total_messages = len(messages)
            
            # 安全的Bot消息统计
            bot_messages = 0
            user_messages = 0
            valid_messages = 0
            
            for i, msg in enumerate(messages):
                try:
                    if not hasattr(msg, 'user_info') or not msg.user_info:
                        logger.debug(f"[DEBUG] 消息{i}缺少user_info")
                        continue
                    
                    user_id = str(msg.user_info.user_id) if msg.user_info.user_id else ""
                    if user_id == bot_qq:
                        bot_messages += 1
                    else:
                        user_messages += 1
                    valid_messages += 1
                    
                except Exception as msg_error:
                    logger.debug(f"[DEBUG] 处理消息{i}时出错: {msg_error}")
                    continue
            
            error_context = "聊天统计阶段"
            # 安全的聊天ID统计
            chat_ids = set()
            for msg in messages:
                try:
                    if hasattr(msg, 'chat_id') and msg.chat_id:
                        chat_ids.add(msg.chat_id)
                except Exception as chat_error:
                    logger.debug(f"[DEBUG] 获取chat_id时出错: {chat_error}")
                    continue
            
            active_chats = len(chat_ids)
            
            # 数据一致性检查
            if valid_messages != (bot_messages + user_messages):
                logger.warning(f"[DEBUG] 消息统计不一致: 有效消息{valid_messages}, Bot消息{bot_messages}, 用户消息{user_messages}")
            
            logger.info(f"[DEBUG] 日期统计完成 - {date}: 总消息{total_messages}, 有效{valid_messages}, Bot{bot_messages}, 用户{user_messages}, 聊天{active_chats}")
            
            return {
                'total_messages': total_messages,
                'bot_messages': bot_messages,
                'user_messages': user_messages,
                'active_chats': active_chats,
                'context_desc': context_desc,
                'valid_messages': valid_messages,
                'data_quality': 'good' if valid_messages == total_messages else 'partial'
            }
            
        except ValueError as ve:
            logger.error(f"[DEBUG] 参数验证失败 ({error_context}): {ve}")
            return {
                'total_messages': 0,
                'bot_messages': 0,
                'user_messages': 0,
                'active_chats': 0,
                'context_desc': f'【参数错误】',
                'valid_messages': 0,
                'data_quality': 'error',
                'error_detail': str(ve)
            }
        except Exception as e:
            logger.error(f"[DEBUG] 获取日期统计失败 ({error_context}): {e}")
            logger.error(f"[DEBUG] 错误详情: 日期={date}, Bot QQ={bot_qq}, 阶段={error_context}")
            return {
                'total_messages': 0,
                'bot_messages': 0,
                'user_messages': 0,
                'active_chats': 0,
                'context_desc': f'【{error_context}失败】',
                'valid_messages': 0,
                'data_quality': 'error',
                'error_detail': str(e)
            }

    def _build_debug_info(self, bot_qq: str, bot_nickname: str, user_stats: List[Dict], date_stats: Dict, date: str) -> str:
        """
        构建调试信息文本
        
        将各种统计数据组织成用户友好的调试信息文本。
        
        Args:
            bot_qq (str): Bot的QQ号
            bot_nickname (str): Bot的昵称
            user_stats (List[Dict]): 用户活跃度统计
            date_stats (Dict): 日期消息统计
            date (str): 分析的日期
        
        Returns:
            str: 格式化的调试信息文本
        """
        debug_text = f"""🔍 Bot消息读取调试 ({date})：

🤖 Bot信息：
- QQ号: {bot_qq}
- 昵称: {bot_nickname}

📊 最近7天消息统计："""
        
        for user in user_stats[:5]:
            is_bot = "🤖" if user['is_identified_as_bot'] else "👤"
            debug_text += f"\n{is_bot} {user['nickname']} ({user['user_id']}): {user['message_count']}条"
        
        identified_bot_count = sum(1 for user in user_stats if user['is_identified_as_bot'])
        debug_text += f"\n\n✅ 识别为Bot的用户: {identified_bot_count}个"
        
        debug_text += f"\n\n📅 {date} 消息统计 {date_stats['context_desc']}："
        debug_text += f"\n- 活跃聊天: {date_stats['active_chats']}个"
        debug_text += f"\n- 用户消息: {date_stats['user_messages']}条"
        debug_text += f"\n- Bot消息: {date_stats['bot_messages']}条"
        
        return debug_text

    async def _show_specific_diary(self, diary_list: List[Dict], index: int, date: str):
        """
        显示指定编号的日记内容
        
        Args:
            diary_list (List[Dict]): 日记列表
            index (int): 日记编号（从0开始）
            date (str): 日期字符串
        """
        if 0 <= index < len(diary_list):
            diary = diary_list[index]
            content = diary.get("diary_content", "")
            word_count = diary.get("word_count", 0)
            gen_time = datetime.datetime.fromtimestamp(diary.get("generation_time", 0))
            status = "✅已发布" if diary.get("is_published_qzone", False) else "❌未发布"
            await self.send_text(
                f"📖 {date} 日记 {index+1} ({gen_time.strftime('%H:%M')}) | {word_count}字 | {status}:\n\n{content}"
            )
        else:
            await self.send_text("❌ 编号无效，请输入正确编号")

    async def _show_diary_list(self, diary_list: List[Dict], date: str):
        """
        显示日记列表
        
        Args:
            diary_list (List[Dict]): 日记列表
            date (str): 日期字符串
        """
        diary_list_text = []
        for idx, diary in enumerate(diary_list, 1):
            gen_time = datetime.datetime.fromtimestamp(diary.get("generation_time", 0))
            word_count = diary.get("word_count", 0)
            status = "✅已发布" if diary.get("is_published_qzone", False) else "❌未发布"
            diary_list_text.append(f"{idx}. {gen_time.strftime('%H:%M')} | {word_count}字 | {status}")

        await self.send_text(
            f"📅 {date} 的日记列表:\n" + "\n".join(diary_list_text) +
            "\n\n输入 /diary view {日期} {编号} 查看具体内容"
        )

    async def _generate_diary_with_50k_limit(self, diary_action, date: str, messages: List[Any]) -> Tuple[bool, str]:
        """
        使用50k强制截断生成日记
        
        这是一个专门用于手动命令的日记生成方法，使用50k token限制来确保
        即使在大量消息的情况下也能正常生成日记。
        
        Args:
            diary_action: 日记生成Action实例
            date (str): 要生成日记的日期
            messages (List[Any]): 消息列表
        
        Returns:
            Tuple[bool, str]: 成功标志和结果内容
                - bool: 是否生成成功
                - str: 生成的日记内容或错误信息
        
        Note:
            - 强制使用50k token限制，确保兼容性
            - 包含完整的日记生成流程
            - 自动保存生成的日记
        """
        try:
            # 1. 获取bot人设
            personality = await diary_action.get_bot_personality()
            
            # 2. 构建时间线
            timeline = diary_action.build_chat_timeline(messages)
            
            # 3. 强制50k截断
            max_tokens = DiaryConstants.TOKEN_LIMIT_50K
            current_tokens = diary_action.estimate_token_count(timeline)
            if current_tokens > max_tokens:
                timeline = diary_action.truncate_timeline_by_tokens(timeline, max_tokens)
            
            # 4. 生成天气信息
            weather = diary_action.get_weather_by_emotion(messages)
            date_with_weather = diary_action.get_date_with_weather(date, weather)
            
            # 5. 生成prompt
            target_length = diary_action.get_config("qzone_publishing.qzone_word_count", DiaryConstants.DEFAULT_QZONE_WORD_COUNT)
            
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
            use_custom_model = diary_action.get_config("custom_model.use_custom_model", False)
            
            if use_custom_model:
                success, diary_content = await diary_action.generate_with_custom_model(prompt)
            else:
                success, diary_content = await diary_action.generate_with_default_model(prompt, timeline)
            
            if not success or not diary_content:
                return False, diary_content or "模型生成日记失败"
            
            # 7. 字数控制
            max_length = diary_action.get_config("qzone_publishing.qzone_word_count", DiaryConstants.DEFAULT_QZONE_WORD_COUNT)
            if max_length > DiaryConstants.MAX_DIARY_LENGTH:
                max_length = DiaryConstants.MAX_DIARY_LENGTH
            if len(diary_content) > max_length:
                diary_content = diary_action.smart_truncate(diary_content, max_length)
            
            # 8. 保存到JSON文件
            diary_record = {
                "date": date,
                "diary_content": diary_content,
                "word_count": len(diary_content),
                "generation_time": time.time(),
                "weather": weather,
                "bot_messages": getattr(diary_action, '_timeline_stats', {}).get('bot_messages', 0),
                "user_messages": getattr(diary_action, '_timeline_stats', {}).get('user_messages', 0),
                "is_published_qzone": False,
                "qzone_publish_time": None,
                "status": "生成成功",
                "error_message": ""
            }
            
            await diary_action.storage.save_diary(diary_record)
            return True, diary_content
            
        except Exception as e:
            logger.error(f"生成日记失败: {e}")
            return False, f"生成日记时出错: {str(e)}"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """
        执行日记管理命令
        
        这是命令处理的主入口方法，负责解析用户输入的子命令并分发到相应的处理逻辑。
        包含完整的权限检查、参数验证和错误处理。
        
        Returns:
            Tuple[bool, Optional[str], bool]: 执行结果
                - bool: 是否执行成功
                - Optional[str]: 结果消息或错误信息
                - bool: 是否阻止后续处理
        
        支持的子命令:
            - generate: 手动生成日记
            - list: 查看日记列表和统计
            - view: 查看具体日记内容
            - debug: 显示调试信息
            - help: 显示帮助信息
        
        权限控制:
            - generate, list, debug, help: 仅管理员可用
            - view: 所有用户可用
            - 群聊中无权限时静默处理
            - 私聊中无权限时显示提示
        
        Note:
            该方法包含了所有之前修复的问题和优化，确保稳定运行。
        """
        action = self.matched_groups.get("action")
        param = self.matched_groups.get("param")
        
        try:
            # 获取管理员QQ列表
            admin_qqs = [str(admin_id) for admin_id in self.get_config("plugin.admin_qqs", [])]
            
            # 获取用户ID
            user_id = str(self.message.message_info.user_info.user_id)
            
            # view 命令允许所有用户使用，其他命令需要管理员权限
            if action != "view":
                has_permission = user_id in admin_qqs
                
                if not has_permission:
                    # 检测是否为群聊
                    is_group_chat = self.message.message_info.group_info is not None
                    
                    if is_group_chat:
                        # 群聊内:静默处理,阻止后续处理
                        return False, "无权限", True
                    else:
                        # 私聊内:返回无权限提示,阻止后续处理
                        await self.send_text("❌ 您没有权限使用此命令。")
                        return False, "无权限", True

            if action == "generate":
                # 生成日记（忽略黑白名单，50k强制截断）
                try:
                    date = _format_date_str(param if param else datetime.datetime.now())
                except ValueError as e:
                    await self.send_text(f"❌ 日期格式错误: {str(e)}\n\n💡 正确的日期格式示例:\n• 2025-08-24\n• 2025/08/24\n• 2025.08.24\n\n📝 如果不指定日期，将默认生成今天的日记")
                    return False, "日期格式错误", True
                
                await self.send_text(f"🔄 正在生成 {date} 的日记...")
                
                # 直接获取所有消息，忽略黑白名单配置
                try:
                    date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
                    start_time = date_obj.timestamp()
                    current_time = datetime.datetime.now()
                    if current_time.strftime("%Y-%m-%d") == date:
                        end_time = current_time.timestamp()
                    else:
                        end_time = (date_obj + datetime.timedelta(days=1)).timestamp()
                    
                    # 根据环境检测获取消息
                    messages, context_desc = await self._get_messages_with_context_detection(date)
                    logger.info(f"generate指令环境检测: {context_desc}, 获取到{len(messages)}条消息")
                    
                    min_message_count = DiaryConstants.MIN_MESSAGE_COUNT  # 硬编码最少消息数
                    if len(messages) < min_message_count:
                        await self.send_text(f"❌ {date} {context_desc} 消息数量不足({len(messages)}条),无法生成日记")
                        return False, "消息数量不足", True
                    
                    # 创建日记生成器
                    diary_action = DiaryGeneratorAction(
                        action_data={"date": date, "target_chats": [], "is_manual": True},
                        reasoning="手动生成日记",
                        cycle_timers={},
                        thinking_id="manual_diary",
                        chat_stream=self.message.chat_stream,
                        log_prefix="[DiaryManage]",
                        plugin_config=self.plugin_config,
                        action_message=None
                    )
                    
                    # 使用50k强制截断生成日记
                    success, result = await self._generate_diary_with_50k_limit(diary_action, date, messages)
                    
                    if success:
                        await self.send_text(f"✅ 日记生成成功！\n\n📖 {date}:\n{result}")
                        
                        await self.send_text("📱 正在发布到QQ空间...")
                        qzone_success = await diary_action._publish_to_qzone(result, date)
                        
                        if qzone_success:
                            await self.send_text("🎉 已成功发布到QQ空间！")
                        else:
                            await self.send_text("⚠️ QQ空间发布失败,可能原因:\n1. Napcat服务未启动\n2. 端口配置错误\n3. QQ空间权限问题\n4. Bot账号配置错误")
                    else:
                        await self.send_text(f"❌ 生成失败:{result}")
                    return success, result, True
                    
                except Exception as e:
                    await self.send_text(f"❌ 生成日记时出错:{str(e)}")
                    return False, f"生成出错: {str(e)}", True
                
            elif action == "list":
                param = self.matched_groups.get("param")
                
                if param == "all":
                    # 显示详细统计和趋势分析
                    stats = await self.storage.get_stats()
                    diaries = await self.storage.list_diaries(limit=0)
                    
                    if diaries:
                        # 计算发布统计
                        success_count = sum(1 for diary in diaries if diary.get("is_published_qzone", False))
                        failed_count = len(diaries) - success_count
                        success_rate = (success_count / len(diaries) * 100) if diaries else 0
                        
                        # 计算日期范围
                        dates = [diary.get("date", "") for diary in diaries if diary.get("date")]
                        dates.sort()
                        date_range = f"{dates[0]} ~ {dates[-1]}" if len(dates) > 1 else dates[0] if dates else "无"
                        
                        # 计算最长最短日记
                        max_diary = max(diaries, key=lambda x: x.get('word_count', 0))
                        min_diary = min(diaries, key=lambda x: x.get('word_count', 0))
                        
                        latest_time = datetime.datetime.fromtimestamp(max(diaries, key=lambda x: x.get('generation_time', 0)).get('generation_time', 0))
                        
                        # 计算下次定时任务时间
                        next_schedule = await self._get_next_schedule_time()
                        
                        # 计算本周统计
                        weekly_stats = await self._get_weekly_stats(diaries)
                        
                        stats_text = f"""📚 日记概览:

📊 详细统计:
📖 总日记数: {stats['total_count']}篇
📝 总字数: {stats['total_words']}字 (平均: {stats['avg_words']}字/篇)
📅 日期范围: {date_range} ({len(set(dates))}天)
📱 发布统计: {success_count}篇成功, {failed_count}篇失败 (成功率: {success_rate:.1f}%)
🕐 最近生成: {latest_time.strftime('%Y-%m-%d %H:%M')}
⏰ 下次定时: {next_schedule}

📈 趋势分析:
📝 本周平均: {weekly_stats['avg_words']}字/篇 ({weekly_stats['trend']})
📱 本周发布: {weekly_stats['success_count']}/{weekly_stats['total_count']}篇成功 ({weekly_stats['success_rate']:.0f}%)
🔥 最长日记: {max_diary.get('date', '无')} ({max_diary.get('word_count', 0)}字)
📏 最短日记: {min_diary.get('date', '无')} ({min_diary.get('word_count', 0)}字)"""
                        await self.send_text(stats_text)
                    else:
                        await self.send_text("📭 还没有任何日记记录")
                    
                    return True, "详细统计完成", True
                    
                elif param and re.match(r'\d{4}-\d{1,2}-\d{1,2}', param):
                    # 显示指定日期的日记概况
                    date = _format_date_str(param)
                    date_diaries = await self.storage.get_diaries_by_date(date)
                    
                    if date_diaries:
                        # 计算当天统计
                        total_words = sum(diary.get("word_count", 0) for diary in date_diaries)
                        avg_words = total_words // len(date_diaries) if date_diaries else 0
                        success_count = sum(1 for diary in date_diaries if diary.get("is_published_qzone", False))
                        failed_count = len(date_diaries) - success_count
                        success_rate = (success_count / len(date_diaries) * 100) if date_diaries else 0
                        
                        # 生成时间信息
                        times = [datetime.datetime.fromtimestamp(diary.get("generation_time", 0)) for diary in date_diaries]
                        earliest_time = min(times).strftime('%H:%M')
                        latest_time = max(times).strftime('%H:%M')
                        
                        # 构建日记列表
                        diary_list = []
                        for i, diary in enumerate(date_diaries, 1):
                            gen_time = datetime.datetime.fromtimestamp(diary.get("generation_time", 0))
                            word_count = diary.get("word_count", 0)
                            status = "✅已发布" if diary.get("is_published_qzone", False) else "❌发布失败"
                            diary_list.append(f"{i}. {gen_time.strftime('%H:%M')} ({word_count}字) {status}")
                        
                        date_text = f"""📅 {date} 日记概况:

📝 当天日记: 共{len(date_diaries)}篇
{chr(10).join(diary_list)}

📊 当天统计:
📝 总字数: {total_words}字(平均: {avg_words}字/篇)
📱 发布状态: {success_count}篇成功, {failed_count}篇失败 (成功率: {success_rate:.1f}%)
🕐 最新生成: {latest_time}
⏰ 最早生成: {earliest_time}

💡 查看具体内容:
🌐 QQ空间: 查看已发布的日记内容
📁 本地文件: plugins/diary_plugin/data/diaries/{date}_*.json"""
                        await self.send_text(date_text)
                    else:
                        await self.send_text(f"📭 没有找到 {date} 的日记")
                    return True, "指定日期概况完成", True
                    
                else:
                    # 显示基础概览（统计 + 最近10篇）
                    stats = await self.storage.get_stats()
                    diaries = await self.storage.list_diaries(limit=10)
                    
                    if diaries:
                        # 构建日记列表
                        diary_list = []
                        for diary in diaries:
                            date = diary.get("date", "")
                            word_count = diary.get("word_count", 0)
                            status = "✅已发布" if diary.get("is_published_qzone", False) else "❌发布失败"
                            diary_list.append(f"📅 {date} ({word_count}字) {status}")
                        
                        overview_text = f"""📚 日记概览:

📊 统计信息:
📖 总日记数: {stats['total_count']}篇
📝 总字数: {stats['total_words']}字
📏 平均字数: {stats['avg_words']}字/篇
📅 最新日记: {stats['latest_date']}

📋 最近日记 (10篇):
{chr(10).join(diary_list)}

💡 提示: 使用 /diary list [日期] 查看指定日期概况"""
                        
                        await self.send_text(overview_text)
                    else:
                        await self.send_text("📭 还没有任何日记记录")
                    
                    return True, "日记概览完成", True
                
            elif action == "debug":
                # 调试命令：显示Bot消息读取调试信息
                debug_stage = "初始化"
                try:
                    # 参数验证和日期格式化
                    debug_stage = "日期解析"
                    try:
                        date = _format_date_str(param if param else datetime.datetime.now())
                        logger.info(f"[DEBUG] 开始调试分析: 日期={date}")
                    except ValueError as date_error:
                        error_msg = f"❌ 调试失败: 日期格式错误\n\n📅 错误详情: {str(date_error)}\n\n💡 请使用正确的日期格式，如: 2025-01-15"
                        await self.send_text(error_msg)
                        return False, "日期格式错误", True
                    
                    # 获取Bot配置信息
                    debug_stage = "Bot配置获取"
                    try:
                        bot_qq = str(config_api.get_global_config("bot.qq_account", ""))
                        bot_nickname = config_api.get_global_config("bot.nickname", "麦麦")
                        
                        if not bot_qq:
                            logger.warning("[DEBUG] Bot QQ号未配置")
                            bot_qq = "未配置"
                        logger.debug(f"[DEBUG] Bot配置: QQ={bot_qq}, 昵称={bot_nickname}")
                    except Exception as config_error:
                        logger.error(f"[DEBUG] Bot配置获取失败: {config_error}")
                        error_msg = f"❌ 调试失败: 无法获取Bot配置信息\n\n🔧 错误详情: {str(config_error)}\n\n💡 请检查Bot配置是否正确"
                        await self.send_text(error_msg)
                        return False, "配置获取失败", True
                    
                    # 获取最近7天消息统计
                    debug_stage = "历史消息获取"
                    try:
                        week_ago = time.time() - 7 * 24 * 3600
                        recent_messages = message_api.get_messages_by_time(
                            start_time=week_ago,
                            end_time=time.time(),
                            filter_mai=False  # 包含Bot消息
                        )
                        
                        if not isinstance(recent_messages, list):
                            logger.warning(f"[DEBUG] 历史消息API返回非列表类型: {type(recent_messages)}")
                            recent_messages = []
                        
                        logger.info(f"[DEBUG] 获取最近7天消息: {len(recent_messages)}条")
                        
                    except Exception as history_error:
                        logger.error(f"[DEBUG] 历史消息获取失败: {history_error}")
                        recent_messages = []
                    
                    # 分析用户活跃度
                    debug_stage = "用户活跃度分析"
                    try:
                        user_stats = self._analyze_user_activity(recent_messages, bot_qq)
                        logger.info(f"[DEBUG] 用户活跃度分析完成: {len(user_stats)}个用户")
                    except Exception as activity_error:
                        logger.error(f"[DEBUG] 用户活跃度分析失败: {activity_error}")
                        user_stats = []
                    
                    # 获取指定日期消息统计
                    debug_stage = "当日消息统计"
                    try:
                        date_stats = await self._get_date_message_stats(date, bot_qq)
                        logger.info(f"[DEBUG] 当日消息统计完成: 数据质量={date_stats.get('data_quality', 'unknown')}")
                    except Exception as stats_error:
                        logger.error(f"[DEBUG] 当日消息统计失败: {stats_error}")
                        date_stats = {
                            'total_messages': 0,
                            'bot_messages': 0,
                            'user_messages': 0,
                            'active_chats': 0,
                            'context_desc': '【统计失败】',
                            'data_quality': 'error',
                            'error_detail': str(stats_error)
                        }
                    
                    # 构建并发送调试信息
                    debug_stage = "结果构建"
                    try:
                        debug_text = self._build_debug_info(bot_qq, bot_nickname, user_stats, date_stats, date)
                        
                        # 添加数据质量报告
                        quality_info = ""
                        if date_stats.get('data_quality') == 'error':
                            quality_info = f"\n\n⚠️ 数据质量警告:\n❌ 统计过程出现错误: {date_stats.get('error_detail', '未知错误')}"
                        elif date_stats.get('data_quality') == 'partial':
                            quality_info = f"\n\n⚠️ 数据质量提醒:\n📊 部分消息数据不完整，统计结果可能不准确"
                        elif len(user_stats) == 0 and len(recent_messages) > 0:
                            quality_info = f"\n\n⚠️ 分析警告:\n📊 用户活跃度分析失败，但历史消息存在"
                        
                        await self.send_text(debug_text + quality_info)
                        logger.info(f"[DEBUG] 调试信息发送完成")
                        
                    except Exception as build_error:
                        logger.error(f"[DEBUG] 调试信息构建失败: {build_error}")
                        # 发送简化的错误报告
                        simple_report = f"""🔍 调试信息 (简化版):
🤖 Bot信息: {bot_nickname} ({bot_qq})
📅 分析日期: {date}
📊 当日消息: {date_stats.get('total_messages', 0)}条
❌ 详细信息构建失败: {str(build_error)}

💡 建议检查日志获取更多详情"""
                        await self.send_text(simple_report)
                    
                    return True, "调试信息完成", True
                    
                except Exception as e:
                    logger.error(f"[DEBUG] 调试命令在{debug_stage}阶段失败: {e}")
                    logger.error(f"[DEBUG] 完整错误信息: {str(e)}")
                    
                    # 根据失败阶段提供不同的错误信息
                    stage_messages = {
                        "初始化": "初始化过程出现问题",
                        "日期解析": "日期解析失败",
                        "Bot配置获取": "Bot配置信息获取失败",
                        "历史消息获取": "历史消息获取失败",
                        "用户活跃度分析": "用户活跃度分析失败",
                        "当日消息统计": "当日消息统计失败",
                        "结果构建": "调试结果构建失败"
                    }
                    
                    stage_desc = stage_messages.get(debug_stage, "未知阶段")
                    error_msg = f"""❌ 调试信息获取失败
                    
🔧 失败阶段: {stage_desc}
📝 错误详情: {str(e)}
📅 分析日期: {param if param else '今天'}

💡 解决建议:
1. 检查日期格式是否正确 (YYYY-MM-DD)
2. 确认Bot配置是否完整
3. 检查数据库连接是否正常
4. 查看详细日志获取更多信息

🆘 如问题持续，请联系管理员并提供此错误信息"""
                    
                    await self.send_text(error_msg)
                    return False, f"调试失败({debug_stage})", True

            elif action == "view":
                # 查看日记命令：支持所有用户使用（不需要管理员权限）
                try:
                    args = param.split() if param else []
                    date = _format_date_str(args[0] if args else datetime.datetime.now())
                    diary_list = await self.storage.get_diaries_by_date(date)
                    
                    if not diary_list:
                        await self.send_text(f"📭 没有找到 {date} 的日记")
                        return True, "查看完成", True
                    
                    # 按生成时间排序
                    diary_list.sort(key=lambda x: x.get('generation_time', 0))
                    # 检查是否指定了编号
                    if len(args) > 1 and args[1].isdigit():
                        await self._show_specific_diary(diary_list, int(args[1]) - 1, date)
                    else:
                        await self._show_diary_list(diary_list, date)
                    
                    return True, "查看完成", True
                    
                except ValueError as e:
                    await self.send_text(f"❌ 日期格式错误: {str(e)}")
                    return False, "日期格式错误", True
                except Exception as e:
                    logger.error(f"查看日记失败: {e}")
                    await self.send_text("❌ 查看日记时出错")
                    return False, "查看失败", True

            elif action == "help":
                help_text = """📖 日记插件帮助

🔧 可用命令:
/diary generate [日期] - 生成指定日期的日记（默认今天）

/diary list - 显示基础概览（统计 + 最近10篇）
/diary list [日期] - 显示指定日期的日记概况
/diary list all - 显示详细统计和趋势分析

/diary view - 查看当天日记列表
/diary view [日期] - 查看指定日期的日记列表
/diary view [日期] [编号] - 查看指定日期的第N条日记内容

/diary debug [日期] - 显示Bot消息读取调试信息（默认今天）

/diary help - 显示此帮助信息

📅 日期格式: YYYY-MM-DD 或 YYYY-M-D（如: 2025-08-24 或 2025-8-24）

💡 查看日记内容:
🌐 QQ空间: 查看已发布的日记
📁 本地文件: plugins/diary_plugin/data/diaries/

📝 权限说明:
- generate, list, debug, help:仅管理员可用
- view: 所有用户可用

🆕 新功能说明:
- 群聊中使用generate命令：只生成当前群的日记
- 私聊中使用generate命令：生成全局日记（所有聊天）"""
                await self.send_text(help_text)
                return True, "帮助信息完成", True
                
            else:
                await self.send_text("❓ 未知的日记命令。使用 /diary help 查看可用命令。")
                return False, "未知命令", True
                
        except Exception as e:
            logger.error(f"日记管理命令出错: {e}")
            await self.send_text(f"❌ 命令执行出错:{str(e)}")
            return False, f"命令出错: {str(e)}", True