import asyncio
import datetime
import time
import json
import random
import os
import re
from typing import List, Tuple, Type, Dict, Any, Optional

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseAction,
    BaseCommand,
    BaseTool,
    ComponentInfo,
    ActionActivationType,
    ConfigField,
    ToolParamType
)
from src.plugin_system.apis import (
    config_api,
    llm_api,
    send_api,
    get_logger,
    message_api  # 使用内置消息API
)

logger = get_logger("diary_plugin")

# ===== 常量定义 =====
class DiaryConstants:
    """日记插件常量"""
    MIN_MESSAGE_COUNT = 3
    TOKEN_LIMIT_50K = 50000
    TOKEN_LIMIT_126K = 126000
    MAX_DIARY_LENGTH = 8000
    DEFAULT_QZONE_WORD_COUNT = 300

def _format_date_str(date_input: Any) -> str:
    """统一的日期格式化函数,确保YYYY-MM-DD格式"""
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
            logger.warning(f"日期格式化失败: {e}")
    
    # 最后的后备方案
    logger.warning(f"无法格式化日期: {date_input}, 使用今天日期")
    return datetime.datetime.now().strftime("%Y-%m-%d")

# ===== 虚拟ChatStream类 =====

class MockChatStream:
    """虚拟聊天流,用于定时任务中的Action初始化"""
    
    def __init__(self):
        self.stream_id = "diary_scheduled_task"
        self.platform = "qq"
        self.group_info = None
        self.user_info = None

# ===== QQ空间API类 =====

class DiaryQzoneAPI:
    """日记插件专用的QQ空间API"""
    
    def __init__(self):
        self.cookies = {}
        self.gtk2 = ''
        
        # 安全的uin获取
        try:
            uin = config_api.get_global_config('bot.qq_account', 0)
            self.uin = int(uin) if uin else 0
        except (ValueError, TypeError):
            logger.warning("无法获取有效的QQ账号，使用默认值0")
            self.uin = 0
        
        # 确保uin有效
        if self.uin <= 0:
            logger.warning(f"QQ账号无效({self.uin})，cookie功能可能异常")
        
        # 使用更安全的文件名
        safe_uin = max(self.uin, 0)  # 确保非负数
        self.cookie_file = os.path.join(os.path.dirname(__file__), "data", f"qzone_cookies_{safe_uin}.json")
    
    async def _fetch_cookies_by_napcat(self, host: str, port: str, napcat_token: str = "") -> dict:
        """通过Napcat自动获取cookies"""
        import httpx
        
        url = f"http://{host}:{port}/get_cookies"
        domain = "user.qzone.qq.com"
        
        try:
            headers = {"Content-Type": "application/json"}
            if napcat_token:
                headers["Authorization"] = f"Bearer {napcat_token}"
            
            payload = {"domain": domain}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                
                if resp.status_code != 200:
                    raise RuntimeError(f"Napcat服务返回错误状态码: {resp.status_code}")
                
                data = resp.json()
                if data.get("status") != "ok" or "cookies" not in data.get("data", {}):
                    raise RuntimeError(f"获取cookie失败: {data}")
                
                cookie_str = data["data"]["cookies"]
                
                # 安全的cookie解析
                cookies = {}
                try:
                    for pair in cookie_str.split("; "):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            cookies[key] = value
                        else:
                            logger.warning(f"跳过格式错误的cookie: {pair}")
                except Exception as parse_error:
                    logger.error(f"Cookie解析失败: {parse_error}")
                    raise RuntimeError(f"Cookie格式错误: {cookie_str}")
                
                return cookies
                
        except Exception as e:
            logger.error(f"通过Napcat获取cookies失败: {e}")
            raise
    
    async def _renew_cookies(self, host: str = "127.0.0.1", port: str = "9998", napcat_token: str = ""):
        """自动更新cookies并保存"""
        try:
            cookie_dict = await self._fetch_cookies_by_napcat(host, port, napcat_token)
            
            cookie_dir = os.path.dirname(self.cookie_file)
            os.makedirs(cookie_dir, exist_ok=True)
            
            if not os.access(cookie_dir, os.W_OK):
                raise PermissionError(f"无法写入目录: {cookie_dir}")
            
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, indent=4, ensure_ascii=False)
            
            self.cookies = cookie_dict
            if 'p_skey' in self.cookies:
                self.gtk2 = self._generate_gtk(self.cookies['p_skey'])
            
            return True
            
        except Exception as e:
            logger.error(f"自动更新cookies失败: {e}")
            try:
                if os.path.exists(self.cookie_file):
                    with open(self.cookie_file, 'r', encoding='utf-8') as f:
                        self.cookies = json.load(f)
                        if 'p_skey' in self.cookies:
                            self.gtk2 = self._generate_gtk(self.cookies['p_skey'])
                    logger.info("使用本地cookies文件")
                    return True
                else:
                    logger.error("本地cookies文件也不存在")
                    return False
            except Exception as load_error:
                logger.error(f"加载本地cookies失败: {load_error}")
                return False
    
    def _generate_gtk(self, skey: str) -> str:
        """生成QQ空间的gtk值"""
        hash_val = 5381
        for i in range(len(skey)):
            hash_val += (hash_val << 5) + ord(skey[i])
        return str(hash_val & 2147483647)
    
    async def publish_diary(self, content: str, napcat_host: str = "127.0.0.1", napcat_port: str = "9998") -> bool:
        """发布日记到QQ空间"""
        try:
            import httpx
            
            cookie_success = await self._renew_cookies(napcat_host, napcat_port)
            if not cookie_success:
                logger.error("无法获取QQ空间cookies")
                return False
            
            if not self.cookies or not self.gtk2:
                logger.error("QQ空间cookies无效")
                return False
            
            publish_url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
            
            post_data = {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "con": content,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": "1",
                "to_sign": "0",
                "hostuin": self.uin,
                "code_version": "1",
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{self.uin}"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    publish_url,
                    params={
                        'g_tk': self.gtk2,
                        'uin': self.uin,
                    },
                    data=post_data,
                    headers={
                        'referer': f'https://user.qzone.qq.com/{self.uin}',
                        'origin': 'https://user.qzone.qq.com',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }, cookies=self.cookies
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if 'tid' in result:
                        return True
                    else:
                        logger.error(f"QQ空间发布失败: {result}")
                        return False
                else:
                    logger.error(f"QQ空间API请求失败: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"发布QQ空间失败: {e}")
            return False

# ===== 聊天ID解析器 =====

class ChatIdResolver:
    """聊天ID解析器 - 将用户友好的配置转换为真实的聊天ID"""
    
    def __init__(self):
        self.cache_file = os.path.join(os.path.dirname(__file__), "data", "chat_mapping.json")
        self.cache = {}
        self.last_config_hash = ""
        
    def _get_config_hash(self, groups: List[str], privates: List[str]) -> str:
        """计算配置的哈希值,用于检测配置变更"""
        import hashlib
        config_str = f"groups:{','.join(sorted(groups))};privates:{','.join(sorted(privates))}"
        return hashlib.md5(config_str.encode()).hexdigest()
    
    def _load_cache(self) -> bool:
        """加载缓存文件"""
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
                filter_mai=False
            )
            return True  # 能成功调用就说明chat_id有效
        except Exception:
            return False
    
    def resolve_filter_mode(self, filter_mode: str, target_chats: List[str], _recursion_depth: int = 0) -> Tuple[str, List[str]]:
        """根据过滤模式和目标列表解析处理策略，防止无限递归"""
        
        # 防止无限递归
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

# ===== JSON存储类 =====

class DiaryStorage:
    """JSON文件存储的日记管理类"""
    
    def __init__(self):
        base_dir = os.path.dirname(__file__)
        self.data_dir = os.path.join(base_dir, "data", "diaries")
        self.index_file = os.path.join(base_dir, "data", "index.json")
        
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)
        
        if not os.access(self.data_dir, os.W_OK):
            logger.warning(f"日记数据目录无写入权限: {self.data_dir}")
        if not os.access(os.path.dirname(self.index_file), os.W_OK):
            logger.warning(f"索引文件目录无写入权限: {os.path.dirname(self.index_file)}")
    
    async def save_diary(self, diary_data: Dict[str, Any], expected_hour: int = None, expected_minute: int = None) -> bool:
        """保存日记到JSON文件"""
        try:
            date = diary_data["date"]
            generation_time = diary_data.get("generation_time", time.time())
            
            if expected_hour is not None and expected_minute is not None:
                filename = f"{_format_date_str(date)}_{expected_hour:02d}{expected_minute:02d}00.json"
            else:
                timestamp = datetime.datetime.fromtimestamp(generation_time)
                filename = f"{_format_date_str(date)}_{timestamp.strftime('%H%M%S')}.json"
            
            file_path = os.path.join(self.data_dir, filename)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(diary_data, f, ensure_ascii=False, indent=2)
            
            await self._update_index(diary_data)
            
            return True
        except Exception as e:
            logger.error(f"保存日记失败: {e}")
            return False
    
    async def get_diary(self, date: str) -> Optional[Dict[str, Any]]:
        """获取指定日期的最新日记"""
        try:
            if not os.path.exists(self.data_dir):
                return None
            
            date_files = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(f"{_format_date_str(date)}_") and filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        date_files.append(diary_data)
            
            if date_files:
                latest_diary = max(date_files, key=lambda x: x.get('generation_time', 0))
                return latest_diary
            
            return None
        except Exception as e:
            logger.error(f"读取日记失败: {e}")
            return None
    
    async def get_diaries_by_date(self, date: str) -> List[Dict[str, Any]]:
        """获取指定日期的所有日记"""
        try:
            if not os.path.exists(self.data_dir):
                return []
            
            date_files = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(f"{_format_date_str(date)}_") and filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        date_files.append(diary_data)
            
            # 按生成时间排序
            date_files.sort(key=lambda x: x.get('generation_time', 0))
            return date_files
        except Exception as e:
            logger.error(f"读取日期日记失败: {e}")
            return []
    
    async def list_diaries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """列出最近的日记"""
        try:
            diary_files = []
            
            if not os.path.exists(self.data_dir):
                return []
            
            for filename in os.listdir(self.data_dir):
                if filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        diary_files.append(diary_data)
            
            diary_files.sort(key=lambda x: x.get('generation_time', 0), reverse=True)
            return diary_files[:limit] if limit > 0 else diary_files
        except Exception as e:
            logger.error(f"列出日记失败: {e}")
            return []
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取日记统计信息"""
        try:
            diaries = await self.list_diaries(limit=0)
            if not diaries:
                return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}
            
            total_count = len(diaries)
            total_words = sum(diary.get("word_count", 0) for diary in diaries)
            avg_words = total_words // total_count if total_count > 0 else 0
            latest_date = max(diaries, key=lambda x: x.get('generation_time', 0)).get('date', '无')
            
            return {
                "total_count": total_count,
                "total_words": total_words,
                "avg_words": avg_words,
                "latest_date": latest_date
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}
    
    async def _update_index(self, diary_data: Dict[str, Any]):
        """更新索引文件"""
        try:
            index_data = {"last_update": time.time(), "total_diaries": 0, "success_count": 0, "failed_count": 0}
            if os.path.exists(self.index_file):
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    index_data = json.load(f)
            
            index_data["last_update"] = time.time()
            
            if os.path.exists(self.data_dir):
                all_files = [f for f in os.listdir(self.data_dir) if f.endswith('.json')]
                success_count = 0
                failed_count = 0
                
                for filename in all_files:
                    file_path = os.path.join(self.data_dir, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            if data.get("is_published_qzone", False):
                                success_count += 1
                            else:
                                failed_count += 1
                    except:
                        failed_count += 1
                
                index_data["success_count"] = success_count
                index_data["failed_count"] = failed_count
                index_data["total_diaries"] = len(all_files)
            
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"更新索引失败: {e}")

# ===== 工具组件 =====

class EmotionAnalysisTool(BaseTool):
    """情感分析工具"""
    
    name = "emotion_analysis"
    description = "分析聊天记录的情感色彩,识别开心、无语、吐槽等情绪"
    parameters = [
        ("messages", ToolParamType.STRING, "聊天记录文本", True, None),
        ("analysis_type", ToolParamType.STRING, "分析类型:emotion(情感)或topic(主题)", False, ["emotion", "topic"])
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """执行情感分析"""
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

# ===== Action组件 =====

class DiaryGeneratorAction(BaseAction):
    """日记生成Action - 使用内置API"""
    
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
        """实时获取bot人设信息"""
        personality_core = config_api.get_global_config("personality.personality_core", "一个机器人")
        personality_side = config_api.get_global_config("personality.personality_side", "")
        reply_style = config_api.get_global_config("personality.reply_style", "")
        
        return {
            "core": personality_core,
            "side": personality_side,
            "style": reply_style
        }

    async def get_daily_messages(self, date: str, target_chats: List[str] = None, end_hour: int = None, end_minute: int = None) -> List[Dict[str, Any]]:
        """获取指定日期的聊天记录（使用内置API）"""
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
                            filter_mai=False,# 不过滤Bot消息
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
                            msg_chat_id = msg.get('chat_id', '')
                            if msg_chat_id not in excluded_chat_ids:
                                all_messages.append(msg)
                        
                        logger.debug(f"黑名单模式:排除了{len(excluded_chat_ids)}个聊天,处理了{len(all_messages)}条消息")
                        
                    except Exception as e:
                        logger.error(f"获取所有消息失败: {e}")
            
            # 按时间排序
            all_messages.sort(key=lambda x: x.get('time', 0))
            
            # 实现min_messages_per_chat过滤逻辑
            min_messages_per_chat = self.get_config("diary_generation.min_messages_per_chat", DiaryConstants.MIN_MESSAGE_COUNT)
            if min_messages_per_chat > 0:
                # 按聊天ID分组消息
                chat_message_counts = {}
                for msg in all_messages:
                    chat_id = msg.get('chat_id', '')
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
                filtered_messages.sort(key=lambda x: x.get('time', 0))
                logger.debug(f"消息过滤: 原始{len(all_messages)}条 → 过滤后{len(filtered_messages)}条 (min_messages_per_chat={min_messages_per_chat})")
                logger.debug(f"聊天过滤: 总聊天{len(chat_message_counts)}个 → 保留{kept_chats}个,过滤{filtered_chats}个")
                return filtered_messages
            
            return all_messages
            
        except Exception as e:
            logger.error(f"获取日期消息失败: {e}")
            return []

    def get_weather_by_emotion(self, messages: List[Dict[str, Any]]) -> str:
        """根据聊天内容的情感分析生成天气"""
        enable_emotion = self.get_config("diary_generation.enable_emotion_analysis", True)
        
        if not enable_emotion or not messages:
            weather_options = ["晴", "多云", "阴", "多云转晴"]
            return random.choice(weather_options)
        
        all_content = " ".join([msg.get('processed_plain_text', '') for msg in messages])
        
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
        """生成带天气的日期字符串,兼容跨平台"""
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

    def build_chat_timeline(self, messages: List[Dict[str, Any]]) -> str:
        """构建完整对话时间线（使用内置API数据）"""
        if not messages:
            return "今天没有什么特别的对话。"
        
        timeline_parts = []
        current_hour = -1
        bot_nickname = config_api.get_global_config("bot.nickname", "麦麦")
        bot_qq_account = str(config_api.get_global_config("bot.qq_account", ""))
        
        bot_message_count = 0
        user_message_count = 0
        
        for msg in messages:
            msg_time = datetime.datetime.fromtimestamp(msg.get('time', 0))
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
            nickname = msg.get('user_nickname', '某人')
            user_id = str(msg.get('user_id', ''))
            content = msg.get('processed_plain_text', '')
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

    def estimate_token_count(self, text: str) -> int:
        """估算文本的token数量"""
        import re
        
        # 中文字符数
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        # 其他字符数
        other_chars = len(text) - chinese_chars
        # 中文约1.5字符=1token,英文约4字符=1token
        estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
        return estimated_tokens

    def truncate_timeline_by_tokens(self, timeline: str, max_tokens: int) -> str:
        """按token数量截断时间线"""
        current_tokens = self.estimate_token_count(timeline)
        
        if current_tokens <= max_tokens:
            return timeline
        
        # 按比例截断
        ratio = max_tokens / current_tokens
        target_length = int(len(timeline) * ratio * 0.95)  # 留5%余量
        
        # 智能截断,保持语句完整
        truncated = timeline[:target_length]
        
        # 找到最后一个完整句子
        for i in range(len(truncated) - 1, len(truncated) // 2, -1):
            if truncated[i] in ['。', '！', '？', '\n']:
                truncated = truncated[:i+1]
                break
        
        logger.debug(f"时间线截断: {current_tokens}→{self.estimate_token_count(truncated)} tokens")
        return truncated + "\n\n[聊天记录过长,已截断]"

    def smart_truncate(self, text: str, max_length: int = DiaryConstants.MAX_DIARY_LENGTH) -> str:
        """智能截断文本,保持语句完整性"""
        if len(text) <= max_length:
            return text
        
        for i in range(max_length - 3, max_length // 2, -1):
            if text[i] in ['。', '！', '？', '~']:
                return text[:i+1]
        
        return text[:max_length-3] + "..."

    async def generate_with_custom_model(self, prompt: str) -> Tuple[bool, str]:
        """使用自定义模型生成日记（参照InternetSearchPlugin）"""
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
            
            # 调用模型
            completion = await client.chat.completions.create(
                model=self.get_config("custom_model.model_name", "Pro/deepseek-ai/DeepSeek-V3"),
                messages=[{"role": "user", "content": prompt}],
                temperature=self.get_config("custom_model.temperature", 0.7),
                timeout=self.get_config("custom_model.api_timeout", 300)
            )
            
            content = completion.choices[0].message.content
            logger.debug(f"自定义模型调用成功: {self.get_config('custom_model.model_name')}")
            return True, content
            
        except Exception as e:
            logger.error(f"自定义模型调用失败: {e}")
            return False, f"自定义模型调用出错: {str(e)}"

    async def generate_with_default_model(self, prompt: str, timeline: str) -> Tuple[bool, str]:
        """使用默认模型生成日记（带126k截断）"""
        try:
            # 默认模型强制126k截断（128k-2k预留）
            max_tokens = DiaryConstants.TOKEN_LIMIT_126K
            current_tokens = self.estimate_token_count(timeline)
            
            if current_tokens > max_tokens:
                logger.debug(f"默认模型:聊天记录超过126k tokens,进行截断")
                # 重新构建截断后的prompt
                truncated_timeline = self.truncate_timeline_by_tokens(timeline, max_tokens)
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
        """发布日记到QQ空间"""
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
        """生成日记的核心逻辑（使用内置API）"""
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
            
            prompt = f"""我是{personality['core']},{personality['side']}
我平时说话的风格是:{personality['style']}

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

我的日记:"""

            # 6. 根据配置选择模型生成
            use_custom_model = self.get_config("custom_model.use_custom_model", False)
            logger.debug(f"模型选择: use_custom_model={use_custom_model}")
            
            if use_custom_model:
                model_name = self.get_config("custom_model.model_name", "未知模型")
                logger.info(f"调用自定义模型: {model_name}")
                # 使用自定义模型（支持用户设置的上下文长度）
                max_context_k = self.get_config("custom_model.max_context_tokens", 256)
                max_context_tokens = (max_context_k * 1000) - 2000  # 自动减去2k预留
                
                current_tokens = self.estimate_token_count(timeline)
                if current_tokens > max_context_tokens:
                    logger.debug(f"自定义模型:聊天记录超过{max_context_k}k tokens,进行截断")
                    truncated_timeline = self.truncate_timeline_by_tokens(timeline, max_context_tokens)
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
        """执行日记生成"""
        date = self.action_data.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
        target_chats = self.action_data.get("target_chats", [])
        
        success, result = await self.generate_diary(date, target_chats)
        
        if success:
            await self.send_text(f"📖 {date} 的日记已生成:\n\n{result}")
            return True, f"成功生成{date}的日记"
        else:
            await self.send_text(f"❌ 日记生成失败:{result}")
            return False, result

# ===== Command组件 =====

class DiaryManageCommand(BaseCommand):
    """日记管理命令"""
    
    command_name = "diary"
    command_description = "日记管理命令集合"
    command_pattern = r"^/diary\s+(?P<action>list|generate|help)(?:\s+(?P<param>\S+))?\s*$"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = DiaryStorage()
    
    async def _get_next_schedule_time(self) -> str:
        """计算下次定时任务时间"""
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
        """计算本周统计数据"""
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

    async def _generate_diary_with_50k_limit(self, diary_action, date: str, messages: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """使用50k强制截断生成日记"""
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
            
            prompt = f"""我是{personality['core']},{personality['side']}
我平时说话的风格是:{personality['style']}

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
        """执行日记管理命令"""
        action = self.matched_groups.get("action")
        param = self.matched_groups.get("param")
        
        
        
        try:
            # 获取管理员QQ列表
            admin_qqs = [str(admin_id) for admin_id in self.get_config("plugin.admin_qqs", [])]
            
            # 获取用户ID
            user_id = str(self.message.message_info.user_info.user_id)
            
            # 权限检查
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
                date = _format_date_str(param if param else datetime.datetime.now())
                
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
                    
                    # 获取所有消息，不受黑白名单限制
                    messages = message_api.get_messages_by_time(
                        start_time=start_time,
                        end_time=end_time,
                        limit=0,
                        filter_mai=False
                    )
                    
                    min_message_count = DiaryConstants.MIN_MESSAGE_COUNT  # 硬编码最少消息数
                    if len(messages) < min_message_count:
                        await self.send_text(f"❌ {date} 消息数量不足({len(messages)}条),无法生成日记")
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
                            await self.send_text("⚠️ QQ空间发布失败,可能原因:\n1. Napcat服务未启动\n2. 端口配置错误\n3. QQ空间权限问题")
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
                
                
                
            elif action == "help":
                help_text = """📖 日记插件帮助

🔧 可用命令:
/diary generate [日期] - 生成指定日期的日记（默认今天）

/diary list - 显示基础概览（统计 + 最近10篇）
/diary list [日期] - 显示指定日期的日记概况
/diary list all - 显示详细统计和趋势分析

/diary help - 显示此帮助信息

📅 日期格式: YYYY-MM-DD 或 YYYY-M-D（如: 2025-08-24 或 2025-8-24）

💡 查看日记内容:
🌐 QQ空间: 查看已发布的日记
📁 本地文件: plugins/diary_plugin/data/diaries/"""
                await self.send_text(help_text)
                return True, "帮助信息完成", True
                
            else:
                await self.send_text("❓ 未知的日记命令。使用 /diary help 查看可用命令。")
                return False, "未知命令", True
                
        except Exception as e:
            logger.error(f"日记管理命令出错: {e}")
            await self.send_text(f"❌ 命令执行出错:{str(e)}")
            return False, f"命令出错: {str(e)}", True

# ===== 定时任务调度器 =====

class DiaryScheduler:
    """日记定时任务调度器"""
    
    def __init__(self, plugin):
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.logger = get_logger("DiaryScheduler")
        self.storage = DiaryStorage()
    
    def _get_timezone_now(self):
        """获取配置时区的当前时间"""
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
        """启动定时任务"""
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
        """停止定时任务"""
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
        """定时任务循环"""
        while self.is_running:
            try:
                now = self._get_timezone_now()
                schedule_time_str = self.plugin.get_config("schedule.schedule_time", "23:30")
                
                schedule_hour, schedule_minute = map(int, schedule_time_str.split(":"))
                today_schedule = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                
                if now >= today_schedule:
                    today_schedule += datetime.timedelta(days=1)
                
                wait_seconds = (today_schedule - now).total_seconds()
                self.logger.debug(f"下次日记生成时间: {today_schedule.strftime('%Y-%m-%d %H:%M:%S')}")
                
                await asyncio.sleep(wait_seconds)
                if self.is_running:
                    await self._generate_daily_diary()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"定时任务出错: {e}")
                await asyncio.sleep(60)

    async def _generate_daily_diary(self):
        """生成每日日记（完全静默）"""
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            
            diary_action = DiaryGeneratorAction(
                action_data={"date": today, "target_chats": [], "is_manual": False},
                reasoning="定时生成日记",
                cycle_timers={},
                thinking_id="scheduled_diary",
                chat_stream=MockChatStream(),
                log_prefix="[ScheduledDiary]",
                plugin_config=self.plugin.config,  # 传递完整配置
                action_message=None
            )
            
            success, result = await diary_action.generate_diary(today)
            
            if success:
                qzone_success = await diary_action._publish_to_qzone(result, today)
                if qzone_success:
                    self.logger.info(f"定时日记生成成功: {today} ({len(result)}字) - QQ空间发布成功")
                else:
                    self.logger.info(f"定时日记生成成功: {today} ({len(result)}字) - QQ空间发布失败")
                    
            else:
                self.logger.error(f"定时日记生成失败: {today} - {result}")
                
        except Exception as e:
            self.logger.error(f"定时生成日记出错: {e}")

# ===== 主插件类 =====

@register_plugin
class DiaryPlugin(BasePlugin):
    """日记插件 - 使用MaiBot内置API的健康版本"""
    
    plugin_name = "diary_plugin"
    enable_plugin = True
    dependencies = []
    python_dependencies = ["httpx", "pytz", "openai"]
    config_file_name = "config.toml"
    
    config_section_descriptions = {
        "plugin": "插件基础配置",
        "diary_generation": "日记生成相关配置",
        "qzone_publishing": "QQ空间发布配置",
        "custom_model": "自定义模型配置（仅支持OpenAI格式）",
        "schedule": "定时任务配置"
    }
    
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="2.0.0", description="配置版本"),
            "admin_qqs": ConfigField(type=list, default=[], description="管理员QQ号列表")
        },
        "diary_generation": {
            "min_message_count": ConfigField(type=int, default=3, description="最少消息总数"),
            "min_messages_per_chat": ConfigField(type=int, default=3, description="每聊天最少消息数"),
            "enable_emotion_analysis": ConfigField(type=bool, default=True, description="启用情感分析")
        },
        "qzone_publishing": {
            "qzone_word_count": ConfigField(type=int, default=300, description="QQ空间字数"),
            "napcat_host": ConfigField(type=str, default="127.0.0.1", description="Napcat地址"),
            "napcat_port": ConfigField(type=str, default="9998", description="Napcat端口")
        },
        "custom_model": {
            "use_custom_model": ConfigField(type=bool, default=False, description="使用自定义模型"),
            "api_url": ConfigField(type=str, default="https://api.siliconflow.cn/v1", description="API地址"),
            "api_key": ConfigField(type=str, default="sk-your-siliconflow-key-here", description="API密钥"),
            "model_name": ConfigField(type=str, default="Pro/deepseek-ai/DeepSeek-V3", description="模型名称"),
            "temperature": ConfigField(type=float, default=0.7, description="生成温度"),
            "max_context_tokens": ConfigField(type=int, default=256, description="上下文长度"),
            "api_timeout": ConfigField(type=int, default=300, description="API超时时间")
        },
        "schedule": {
            "schedule_time": ConfigField(type=str, default="23:30", description="定时时间"),
            "timezone": ConfigField(type=str, default="Asia/Shanghai", description="时区"),
            "filter_mode": ConfigField(type=str, default="whitelist", description="过滤模式"),
            "target_chats": ConfigField(type=list, default=[], description="目标聊天列表")
        }
    }
    
    def __init__(self, plugin_dir: str, **kwargs):
        super().__init__(plugin_dir, **kwargs)
        self.scheduler = None
        self.logger = get_logger("DiaryPlugin")
        self.storage = DiaryStorage()
        
        # 显示插件配置状态
        self._log_plugin_status()
        
        # 启动定时任务
        self.scheduler = DiaryScheduler(self)
        asyncio.create_task(self._start_scheduler_after_delay())
    
    def _log_plugin_status(self):
        """显示插件配置状态（info级别）"""
        try:
            # 读取基本配置
            admin_qqs = [str(admin_id) for admin_id in self.get_config("plugin.admin_qqs", [])]
            filter_mode = self.get_config("schedule.filter_mode", "whitelist")
            target_chats = self.get_config("schedule.target_chats", [])
            use_custom_model = self.get_config("custom_model.use_custom_model", False)
            
            # 显示管理员配置
            if admin_qqs:
                self.logger.info(f"管理员已配置: {len(admin_qqs)}个")
            else:
                self.logger.info("管理员未配置,所有用户无权限使用命令")
            
            # 显示过滤模式和定时任务状态
            if filter_mode == "whitelist":
                if target_chats:
                    self.logger.info(f"白名单模式: 已配置{len(target_chats)}个目标聊天,定时任务将启动")
                else:
                    self.logger.info("白名单模式: 目标列表未配置,定时任务已禁用")
            elif filter_mode == "blacklist":
                if target_chats:
                    self.logger.info(f"黑名单模式: 排除{len(target_chats)}个聊天,定时任务将启动")
                else:
                    self.logger.info("黑名单模式: 无排除列表,处理全部聊天,定时任务将启动")
            
            # 显示模型配置
            if use_custom_model:
                model_name = self.get_config("custom_model.model_name", "未知模型")
                api_key = self.get_config("custom_model.api_key", "")
                if api_key and api_key != "sk-your-siliconflow-key-here":
                    self.logger.info(f"自定义模型已启用: {model_name}")
                else:
                    self.logger.info("自定义模型已启用但API密钥未配置,将使用默认模型")
            else:
                self.logger.info("使用系统默认模型")
                
        except Exception as e:
            self.logger.error(f"读取插件配置失败: {e}")
    

    async def _start_scheduler_after_delay(self):
        """延迟启动定时任务"""
        await asyncio.sleep(10)
        if self.scheduler:
            await self.scheduler.start()

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""
        return [
            (DiaryGeneratorAction.get_action_info(), DiaryGeneratorAction),
            (EmotionAnalysisTool.get_tool_info(), EmotionAnalysisTool),
            (DiaryManageCommand.get_command_info(), DiaryManageCommand)
        ]