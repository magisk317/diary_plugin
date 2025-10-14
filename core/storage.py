"""
存储和API模块

本模块包含日记插件的核心存储功能，提供了完整的数据持久化解决方案。

主要功能：
- JSON文件存储管理：提供日记数据的本地存储和检索

模块组件：
- DiaryStorage: JSON文件存储的日记管理类

注意：ChatIdResolver已移至utils模块，提供跨模块共享的聊天ID解析功能

作者: MaiBot日记插件开发团队
版本: 2.1.0
"""

import asyncio
import datetime
import time
import json
import os
import re
import hashlib
import httpx
from typing import List, Tuple, Type, Dict, Any, Optional

from src.plugin_system.apis import (
    config_api,
    get_logger,
    message_api
)

# 导入共享的工具类
from .utils import ChatIdResolver, format_date_str

logger = get_logger("diary_plugin.storage")


class DiaryStorage:
    """
    JSON文件存储的日记管理类
    
    该类提供完整的日记数据持久化解决方案，使用JSON文件格式存储日记内容
    和相关元数据。支持按日期组织、多版本管理和统计分析功能。
    
    主要功能：
    - 日记数据的保存和读取
    - 按日期查询和列表显示
    - 统计信息计算和分析
    - 索引文件维护和更新
    - 文件权限和错误处理
    
    存储结构：
    - data/diaries/: 日记文件存储目录
    - data/index.json: 索引和统计信息文件
    - 文件命名: YYYY-MM-DD_HHMMSS.json
    
    数据格式：
    - date: 日记日期
    - diary_content: 日记正文内容
    - word_count: 字数统计
    - generation_time: 生成时间戳
    - weather: 天气信息
    - bot_messages/user_messages: 消息统计
    - status: 处理状态
    - error_message: 错误信息
    
    使用场景：
    - 定时任务保存生成的日记
    - 命令查询历史日记内容
    - 统计分析日记数据
    - 错误记录和状态跟踪
    
    注意事项：
    - 自动创建必要的目录结构
    - 处理文件权限和IO异常
    - 支持同一天多次生成的版本管理
    """
    
    def __init__(self):
        base_dir = os.path.dirname(__file__)
        self.data_dir = os.path.join(base_dir, "..", "data", "diaries")
        self.index_file = os.path.join(base_dir, "..", "data", "index.json")
        
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
                filename = f"{format_date_str(date)}_{expected_hour:02d}{expected_minute:02d}00.json"
            else:
                timestamp = datetime.datetime.fromtimestamp(generation_time)
                filename = f"{format_date_str(date)}_{timestamp.strftime('%H%M%S')}.json"
            
            file_path = os.path.join(self.data_dir, filename)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(diary_data, f, ensure_ascii=False, indent=2)
            
            await self._update_index(diary_data)
            
            return True
        except Exception as e:
            logger.error(f"保存日记失败: {e}")
            return False
    
    # _format_date_str方法已移至utils模块的format_date_str函数
    
    async def get_diary(self, date: str) -> Optional[Dict[str, Any]]:
        """获取指定日期的最新日记"""
        try:
            if not os.path.exists(self.data_dir):
                return None
            
            date_files = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(f"{format_date_str(date)}_") and filename.endswith('.json'):
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
                if filename.startswith(f"{format_date_str(date)}_") and filename.endswith('.json'):
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
            index_data = {"last_update": time.time(), "total_diaries": 0}
            if os.path.exists(self.index_file):
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    index_data = json.load(f)
            
            index_data["last_update"] = time.time()
            
            if os.path.exists(self.data_dir):
                all_files = [f for f in os.listdir(self.data_dir) if f.endswith('.json')]
                index_data["total_diaries"] = len(all_files)
            
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"更新索引失败: {e}")