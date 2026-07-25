#!/usr/bin/env python3
"""
Автоматический импорт session файлов с пробелами в имени.
Переименовывает файлы и добавляет в БД.
"""
import os
import sys
import sqlite3
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(__file__))

from utils.database import Database
from config import Config

def main():
    # Инициализируем БД
    db = Database('data/bot.db')
    
    sessions_dir = 'data/sessions'
    os.makedirs(sessions_dir, exist_ok=True)
    
    print(f"🔍 Ищу session файлы в {sessions_dir}...")
    
    # Находим все .session файлы
    session_files = list(Path(sessions_dir).glob('*.session'))
    
    if not session_files:
        print("❌ Session файлы не найдены!")
        return
    
    print(f"✅ Найдено {len(session_files)} session файлов\n")
    
    api_id = Config.API_ID
    api_hash = Config.API_HASH
    
    if not api_id or not api_hash:
        print("❌ API_ID или API_HASH не заданы в config!")
        print("⚠️  Используй панель для импорта или добавь credentials в 1.envv")
        return
    
    for session_path in sorted(session_files):
        filename = session_path.name
        
        # Извлекаем номер из имени файла (удаляем всё кроме цифр)
        phone_part = filename.replace('session_', '').replace('.session', '')
        phone_digits = ''.join(filter(str.isdigit, phone_part))
        
        if len(phone_digits) < 10:
            print(f"⚠️  ПРОПУСКАЮ: {filename} (не могу извлечь номер)")
            continue
        
        # Формируем чистое имя БЕЗ пробелов
        clean_phone = phone_digits
        new_session_name = f"session_{clean_phone}"
        new_filename = f"{new_session_name}.session"
        new_path = session_path.parent / new_filename
        
        phone = f"+{phone_digits}"
        
        print(f"📄 {filename}")
        print(f"   └─ Номер: {phone}")
        print(f"   └─ Старое имя: session_{phone_part}")
        print(f"   └─ Новое имя: {new_session_name}")
        
        # Проверяем не существует ли уже такой аккаунт в БД
        existing = next(
            (a for a in db.get_accounts() if str(a.get('phone')) == phone),
            None
        )
        
        if existing:
            print(f"   ⚠️  Аккаунт уже в БД (id={existing['id']}, session={existing['session_name']})")
            
            # Если имя не совпадает, может быть нужно переименовать файл
            if existing['session_name'] != new_session_name:
                # Проверим существует ли файл с нужным именем
                expected_path = session_path.parent / f"{existing['session_name']}.session"
                if not expected_path.exists():
                    print(f"      └─ Переименовываю файл на {existing['session_name']}.session")
                    session_path.rename(expected_path)
                else:
                    print(f"      └─ Файл уже существует: {expected_path.name}")
            print()
            continue
        
        # Переименовываем файл
        if session_path != new_path:
            print(f"   🔄 Переименовываю...")
            
            # ✅ Если целевой файл уже существует, это дубликат
            if new_path.exists():
                print(f"      ⚠️  Целевой файл уже существует, удаляю старый дубликат...")
                session_path.unlink()
                print(f"   ✅ Дубликат удалён, пропускаю добавление в БД!")
                print()
                continue
            
            session_path.rename(new_path)
            print(f"   ✅ Переименовано!")
        
        # Добавляем в БД
        print(f"   📊 Добавляю в БД...")
        try:
            acc_id = db.add_account(phone, new_session_name, api_id, api_hash)
            print(f"   ✅ Добавлено! (id={acc_id})\n")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}\n")
    
    print("="*60)
    print("✅ Импорт завершён!")
    print("\nАккаунты в БД:")
    accounts = db.get_accounts()
    for acc in accounts:
        print(f"  [{acc['id']}] {acc['phone']:20} → {acc['session_name']}")

if __name__ == '__main__':
    main()
