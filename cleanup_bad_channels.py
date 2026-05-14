"""
Удаляет каналы с закрытыми комментариями и initial_discovery мусор из базы
"""
from utils.database import Database

def cleanup():
    db = Database()
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        
        # Удаляем все initial_discovery каналы
        cursor.execute("DELETE FROM found_channels WHERE source = 'initial_discovery'")
        deleted_seeds = cursor.rowcount
        print(f"🗑️ Удалено {deleted_seeds} initial_discovery каналов")
        
        # Удаляем каналы с закрытыми комментариями
        cursor.execute("DELETE FROM found_channels WHERE can_comment = 0")
        deleted_closed = cursor.rowcount
        print(f"🗑️ Удалено {deleted_closed} каналов с закрытыми комментариями")
        
        # Считаем оставшиеся
        cursor.execute("SELECT COUNT(*) FROM found_channels")
        remaining = cursor.fetchone()[0]
        print(f"✅ Осталось {remaining} каналов в базе")

if __name__ == "__main__":
    cleanup()
