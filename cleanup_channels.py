"""
Удаляет каналы без данных о подписчиках или с малым количеством
"""
import sqlite3

conn = sqlite3.connect('data/bot.db')
c = conn.cursor()

MIN_SUBS = 2000  # Минимум подписчиков

# Смотрим статистику
c.execute('SELECT COUNT(*) FROM found_channels WHERE subs_count = 0 OR subs_count IS NULL')
no_subs = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM found_channels WHERE subs_count > 0 AND subs_count < ?', (MIN_SUBS,))
low_subs = c.fetchone()[0]
c.execute('SELECT COUNT(*) FROM found_channels WHERE subs_count >= ?', (MIN_SUBS,))
good_subs = c.fetchone()[0]

print(f"Без данных о подписчиках: {no_subs}")
print(f"Мало подписчиков (<{MIN_SUBS}): {low_subs}")
print(f"Хорошие каналы (>={MIN_SUBS}): {good_subs}")
print("-" * 40)

# Удаляем каналы без подписчиков или с малым количеством
c.execute('DELETE FROM found_channels WHERE subs_count = 0 OR subs_count IS NULL OR subs_count < ?', (MIN_SUBS,))
deleted = c.rowcount

conn.commit()

# Обновляем active_channels.txt - оставляем только каналы которые есть в БД
c.execute('SELECT channel FROM found_channels')
db_channels = set(r[0] for r in c.fetchall())

with open('data/active_channels.txt', 'r', encoding='utf-8') as f:
    active = [line.strip() for line in f if line.strip() and not line.startswith('#')]

new_active = []
for ch in active:
    ch_clean = ch.lstrip('@')
    if ch_clean in db_channels or ch in db_channels:
        new_active.append(ch)

with open('data/active_channels.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_active))

# Итог
c.execute('SELECT COUNT(*) FROM found_channels')
total = c.fetchone()[0]

print(f"\nУдалено из БД: {deleted}")
print(f"Осталось в БД: {total}")
print(f"Осталось в active_channels.txt: {len(new_active)}")

conn.close()
