"""
Миграция каналов из файлов в базу данных.
Запустить один раз для переноса данных.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.database import Database
from config import Config

def migrate():
    db = Database(Config.DATABASE_PATH)
    
    # Загружаем каналы из обоих файлов
    active_channels = []
    channels_to_join = []
    
    if os.path.exists(Config.ACTIVE_CHANNELS_FILE):
        with open(Config.ACTIVE_CHANNELS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    active_channels.append(Config.normalize_channel(line))
    
    if os.path.exists(Config.CHANNELS_TO_JOIN_FILE):
        with open(Config.CHANNELS_TO_JOIN_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    channels_to_join.append(Config.normalize_channel(line))
    
    # Объединяем и убираем дубликаты
    all_channels = list(set(active_channels + channels_to_join))
    
    print(f"📋 Найдено каналов:")
    print(f"   - active_channels.txt: {len(active_channels)}")
    print(f"   - channels_to_join.txt: {len(channels_to_join)}")
    print(f"   - Уникальных: {len(all_channels)}")
    
    # Добавляем в БД
    added = 0
    for channel in all_channels:
        if not channel:
            continue
        is_new = db.add_found_channel(
            channel=channel,
            title=channel,
            keyword="migrated",
            source="file",
            can_comment=True,
            min_subs=0
        )
        if is_new:
            added += 1
        db.update_channel_status(channel, 'active')
    
    print(f"\n✅ Добавлено в БД: {added} новых каналов")
    print(f"📊 Всего каналов в БД: {len(db.get_found_channels(limit=1000, only_open_comments=False))}")
    
    # Переименовываем старые файлы
    if os.path.exists(Config.ACTIVE_CHANNELS_FILE):
        os.rename(Config.ACTIVE_CHANNELS_FILE, Config.ACTIVE_CHANNELS_FILE + '.backup')
        print(f"\n📁 {Config.ACTIVE_CHANNELS_FILE} -> .backup")
    
    if os.path.exists(Config.CHANNELS_TO_JOIN_FILE):
        os.rename(Config.CHANNELS_TO_JOIN_FILE, Config.CHANNELS_TO_JOIN_FILE + '.backup')
        print(f"📁 {Config.CHANNELS_TO_JOIN_FILE} -> .backup")
    
    print("\n🎉 Миграция завершена! Файлы больше не нужны.")

if __name__ == "__main__":
    migrate()
