"""
Конфигурационный модуль для бота
"""
from dotenv import load_dotenv
import os
import json
from typing import List, Dict, Any

load_dotenv('1.envv')


class Config:
    """Класс для управления конфигурацией бота"""
    
    # Обязательные переменные окружения
    REQUIRED_ENV_VARS = ['GEMINI_API_KEY']
    
    @classmethod
    def validate_env(cls) -> List[str]:
        """Проверяет наличие обязательных переменных окружения.
        
        Returns:
            Список отсутствующих переменных (пустой если всё ок)
        """
        missing = []
        for var in cls.REQUIRED_ENV_VARS:
            value = os.getenv(var, "")
            if not value or value.strip() == "":
                missing.append(var)
        return missing
    
    @classmethod
    def ensure_env(cls):
        """Проверяет переменные и выбрасывает ошибку если чего-то не хватает"""
        missing = cls.validate_env()
        if missing:
            raise EnvironmentError(
                f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}. "
                f"Добавьте их в файл 1.envv"
            )
    
    # OpenAI / ProxyAPI (Old - potentially deprecated)
    OPENAI_API_KEY = os.getenv("OpenAI_token", "")
    OPENAI_BASE_URL = "https://openai.api.proxyapi.ru/v1"
    OPENAI_MODEL = "openai/gpt-5-2025-08-07"
    
    # Google Gemini - загружаем из переменных окружения
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "google/gemini-3-flash-preview")
    GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://routerai.ru/api/v1")
    
    # Telegram API
    API_ID = os.getenv('Api_id') or os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('Api_hash') or os.getenv('TELEGRAM_API_HASH')
    
    # API_ID/API_HASH задаются на уровне аккаунта в БД при импорте сессии,
    # а на уровне конфига нужны только как глобальный fallback. Никаких
    # хардкоднутых leaked-ключей TG Desktop здесь не оставляем.
    if API_ID and str(API_ID).strip().isdigit():
        API_ID = int(API_ID)
    else:
        API_ID = None
    if not API_HASH or str(API_HASH).strip() == '':
        API_HASH = None
    
    # Telegram сессия
    SESSION_NAME = 'session_name'
    
    # Владелец бота (для отправки логов)
    OWNER_ID = os.getenv('Owner_id', '@your_username')
    
    # Промпт для персоны девушки-аниме фаната (вынесен в одно место)
    GIRL_PERSONA_SYSTEM = """Ты милая девушка 18-22 лет, общаешься в телеграме.
ВАЖНО:
1. Пиши ОЧЕНЬ КРАТКО. Максимум 1-2 предложения.
2. Пиши как реальный человек с телефона: строчные буквы, иногда без запятых, сленг (пон, спс, жиза).
3. Не используй сложные деепричастные обороты и книжный стиль.
4. Будь милой, но простой."""
    
    # Промпт для генерации комментариев
    COMMENT_PERSONA_SYSTEM = """Ты милая девушка 18-22 лет, которая обожает аниме. 
Общаешься естественно, живо и эмоционально, как настоящий человек, а не бот.

ВАЖНО: 
- Внимательно анализируй КОНТЕНТ поста (текст, изображение, видео)
- Реагируй на КОНКРЕТНЫЕ детали: персонажи, сцены, эмоции, действия
- Упоминай конкретные элементы из поста, а не общие фразы
- Если видишь персонажа - назови его или опиши
- Если видишь сцену - опиши что происходит
- Если есть текст - отреагируй на него конкретно
- Используй эмодзи естественно, не переборщи
- Пиши короткие яркие комментарии до 11 слов
- Будь живой и эмоциональной, но не шаблонной"""
    
    # Промпт для анализа изображений
    IMAGE_ANALYSIS_PROMPT = """Внимательно проанализируй это изображение и напиши короткий яркий комментарий 
(до 11 слов) от лица девушки-аниме фаната.

ВАЖНО:
- Опиши КОНКРЕТНО что видишь на изображении (персонажи, сцены, действия, эмоции)
- Если видишь персонажа аниме - упомяни его или опиши внешность
- Если видишь сцену - опиши что происходит
- Реагируй на детали: позы, выражения лиц, цвета, атмосферу
- Не пиши общие фразы типа "красиво" или "интересно" без контекста
- Будь естественной и эмоциональной, используй эмодзи уместно"""
    
    # Настройки генерации комментариев
    COMMENT_MAX_WORDS = 11
    COMMENT_MAX_TOKENS = 100
    COMMENT_TEMPERATURE = 0.9  # Увеличено для более естественных и разнообразных комментариев
    COMMENT_MAX_POST_AGE_HOURS = 3  # Максимальный возраст поста для комментирования (часы)
    
    # Задержки (в секундах)
    COMMENT_DELAY_MIN = 1
    COMMENT_DELAY_MAX = 3
    JOIN_CHANNEL_DELAY_MIN = 60
    JOIN_CHANNEL_DELAY_MAX = 120
    RESPONSE_DELAY_MIN = 10
    RESPONSE_DELAY_MAX = 30
    
    # Настройки автоответчика
    AUTORESPONDER_ENABLED = True
    AUTORESPONDER_MIN_MESSAGES_BEFORE_PROMOTE = 3  # После скольких сообщений рекламировать канал
    AUTORESPONDER_ONLY_UNKNOWN = True  # Отвечать только незнакомым
    AUTORESPONDER_DIALOG_TIMEOUT_HOURS = 6  # Через сколько часов сбрасывать контекст диалога
    
    # Настройки работы в чатах (группах)
    CHAT_RESPONDER_ENABLED = True  # Отвечать в группах/чатах
    CHAT_RESPOND_TO_MENTIONS = True  # Отвечать когда упоминают бота
    CHAT_RESPOND_TO_DIRECT = True  # Отвечать на прямые обращения
    CHAT_PERIODIC_MESSAGES_ENABLED = True  # Периодически писать сообщения в чаты
    CHAT_PERIODIC_INTERVAL_MIN = 1800  # Минимальный интервал между сообщениями (секунды, 30 минут)
    CHAT_PERIODIC_INTERVAL_MAX = 3600  # Максимальный интервал между сообщениями (секунды, 1 час)
    CHAT_ACTIVE_CHATS_FILE = 'data/active_chats.txt'  # Файл со списком активных чатов
    
    # Настройки поиска
    SEARCH_ENABLED = True
    SEARCH_AUTO_ADD_CHANNELS = True  # Автоматически добавлять найденные каналы
    SEARCH_AUTO_ADD_TO_ACTIVE = True  # Автоматически добавлять найденные каналы в активные для комментирования
    SEARCH_INTERVAL_CYCLES = 20  # Как часто искать каналы (каждые N циклов)
    
    # Настройки реакций
    USE_REACTIONS = True  # Ставить реакции перед комментариями
    REACTION_EMOJI = ['❤️', '🔥', '👍']  # Эмодзи для реакций
    
    # Настройки фильтрации каналов
    SEARCH_MIN_SUBSCRIBERS = 2000  # Минимальное количество подписчиков
    SEARCH_MIN_AVG_VIEWS = 500  # Минимальное среднее кол-во просмотров на пост
    SEARCH_CHECK_LAST_POSTS = 10  # Сколько последних постов проверять на активность
    
    # Настройки поддержки изображений
    SUPPORT_IMAGES = True  # Анализировать изображения через Vision
    IMAGE_MODEL = "openai/gpt-4o"  # Модель для анализа изображений
    
    # Настройки поддержки видео
    SUPPORT_VIDEOS = True  # Анализировать видео через скриншоты
    VIDEO_SCREENSHOT_TIME = 1.0  # Время в секундах для скриншота (1 секунда от начала)
    VIDEO_MAX_SIZE_MB = 100  # Максимальный размер видео для обработки (MB)
    
    
    # Константы режимов работы
    MODE_DELAY_MULT = {
        "powerful": 1.0,
        "neutral": 2.5,
        "chill": 6.0
    }
    MODE_PROBABILITY = {
        "powerful": 1.0,
        "neutral": 0.8,
        "chill": 0.3
    }

    # Inviter settings
    INVITER_DAILY_LIMIT = 40
    INVITER_DELAY_MIN = 30
    INVITER_DELAY_MAX = 120

    # Mass sender settings
    MASS_SEND_HOURLY_LIMIT = 20
    MASS_SEND_DELAY_MIN = 60
    MASS_SEND_DELAY_MAX = 180
    MASS_SEND_ERROR_THRESHOLD = 5

    # Channel creator settings
    CHANNEL_WARMUP_POSTS_COUNT = 5

    # Файлы конфигурации
    CHANNELS_TO_JOIN_FILE = 'data/channels_to_join.txt'
    KEYWORDS_FILE = 'data/keywords.txt'
    ACTIVE_CHANNELS_FILE = 'data/active_channels.txt'
    
    # База данных
    DATABASE_PATH = 'data/bot.db'
    
    @staticmethod
    def normalize_channel(channel: str) -> str:
        """Нормализует название канала - убирает @, извлекает из URL"""
        channel = channel.strip()
        
        # Извлекаем из полной ссылки https://t.me/channel или https://t.me/+hash
        if 't.me/' in channel:
            parts = channel.split('t.me/')
            if len(parts) > 1:
                extracted = parts[-1].split('?')[0]  # Убираем параметры
                # Для приватных ссылок сохраняем + и хэш
                if extracted.startswith('+'):
                    channel = extracted.split('/')[0]  # +hash
                elif extracted.startswith('joinchat/'):
                    # t.me/joinchat/hash -> +hash
                    channel = '+' + extracted[9:].split('/')[0]
                else:
                    channel = extracted.split('/')[0]  # username
        
        # joinchat -> + (для случаев без t.me/)
        if channel.startswith('joinchat/'):
            channel = '+' + channel[9:]
        
        # Убираем @ для публичных каналов (не приватных)
        if not channel.startswith('+'):
            channel = channel.lstrip('@')
        
        return channel
    
    @staticmethod
    def load_channels_from_file(filepath: str) -> List[str]:
        """Загружает список каналов из файла с нормализацией"""
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                channels = []
                for line in f.readlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Нормализуем канал
                        normalized = Config.normalize_channel(line)
                        if normalized:
                            channels.append(normalized)
                return channels
        except Exception as e:
            print(f"Ошибка при загрузке {filepath}: {e}")
            return []
    
    @staticmethod
    def save_channels_to_file(channels: List[str], filepath: str):
        """Сохраняет список каналов в файл с нормализацией"""
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
        try:
            # Нормализуем и убираем дубликаты
            normalized = []
            seen = set()
            for channel in channels:
                norm = Config.normalize_channel(channel)
                if norm and norm not in seen:
                    normalized.append(norm)
                    seen.add(norm)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                for channel in normalized:
                    f.write(f"{channel}\n")
        except Exception as e:
            print(f"Ошибка при сохранении {filepath}: {e}")
    
    @staticmethod
    def load_keywords() -> List[str]:
        """Загружает ключевые слова для поиска"""
        return Config.load_channels_from_file(Config.KEYWORDS_FILE)
    
    @staticmethod
    def load_json_file(filepath: str, default: Any = None) -> Any:
        """Загружает JSON файл"""
        if default is None:
            default = {}
        if not os.path.exists(filepath):
            return default
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка при загрузке {filepath}: {e}")
            return default
    
    @staticmethod
    def save_json_file(data: Any, filepath: str):
        """Сохраняет данные в JSON файл"""
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка при сохранении {filepath}: {e}")

