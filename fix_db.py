#!/usr/bin/env python3
import sqlite3
import os

db_path = 'data/bot.db'

print("🔧 Исправляю БД...")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Проверяем наличие колонок
cursor.execute("PRAGMA table_info(accounts)")
columns = {col[1] for col in cursor.fetchall()}

print(f"✅ Существующие колонки: {columns}\n")

# Добавляем недостающие колонки
cols_to_add = {
    'first_name': 'TEXT',
    'username': 'TEXT',
    'owned_channel': 'TEXT',
}

for col_name, col_type in cols_to_add.items():
    if col_name not in columns:
        try:
            cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type};")
            print(f"✅ Добавлена колонка: {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate" in str(e).lower():
                print(f"⚠️  {col_name} уже существует")
            else:
                print(f"❌ Ошибка при добавлении {col_name}: {e}")

conn.commit()
conn.close()

print("\n✅ БД исправлена!")