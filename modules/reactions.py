"""
Модуль для работы с реакциями на посты
"""
import random
from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from config import Config
from utils.database import Database

class ReactionManager:
    """Класс для управления реакциями на посты"""
    
    def __init__(self, client: TelegramClient, db: Database = None, account_id: int = None):
        self.client = client
        self.db = db
        self.account_id = account_id
    
    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение в БД"""
        if self.account_id and self.db:
            self.db.add_log(self.account_id, level, message)
    
    async def send_reaction(self, entity, message_id: int, emoji: str = None, log_action: bool = True) -> bool:
        """
        Ставит реакцию на пост
        
        Args:
            entity: Entity канала/чата
            message_id: ID сообщения
            emoji: Эмодзи для реакции (если None - случайный из списка)
        
        Returns:
            bool: Успешно ли поставлена реакция
        """
        if not Config.USE_REACTIONS:
            return False
        
        try:
            if emoji is None:
                emoji = random.choice(Config.REACTION_EMOJI)
            
            # Валидация эмодзи - проверяем, что это действительно эмодзи
            # Удаляем пробелы и проверяем длину
            emoji = emoji.strip()
            
            # Проверяем, что эмодзи не пустой и содержит только эмодзи символы
            if not emoji:
                return False
            
            # Проверяем, что эмодзи из списка разрешенных (безопасность)
            if emoji not in Config.REACTION_EMOJI:
                # Если эмодзи не из списка, используем случайный из списка
                emoji = random.choice(Config.REACTION_EMOJI)
            
            # Создаем реакцию
            reaction = ReactionEmoji(emoticon=emoji)
            
            # Отправляем реакцию (Асинхронно)
            await self.client(SendReactionRequest(
                peer=entity,
                msg_id=message_id,
                reaction=[reaction]
            ))
            
            # Увеличиваем счетчик лайков
            if self.db and self.account_id:
                self.db.increment_stat(self.account_id, 'likes')
            
            # Логируем реакцию
            if log_action:
                entity_name = getattr(entity, 'username', None) or getattr(entity, 'title', str(entity))
                self._log(f"❤️ Реакция {emoji} на пост в @{entity_name}")
            
            return True
            
        except Exception as e:
            error_str = str(e)
            ignore_errors = [
                "Invalid reaction",
                "only emoji",
                "reactions_uniq_max",
                "message already has exactly"
            ]
            
            should_ignore = any(ignore in error_str for ignore in ignore_errors)
            
            if not should_ignore:
                print(f"Ошибка при установке реакции: {e}")
            return False

