"""
Модуль массовой фильтрации каналов/чатов.
Позволяет загрузить большую базу каналов (2000+) и автоматически отсеять
мусорные/бесполезные по настраиваемым критериям.
"""
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.errors import (
    FloodWaitError, PeerFloodError, ChannelPrivateError,
    UsernameNotOccupiedError, ChatAdminRequiredError
)

from config import Config
from utils.database import Database
from modules.junk_chat_classifier import JunkChatClassifier


class ChannelFilter:
    """Массовая фильтрация каналов по заданным критериям"""

    def __init__(self, client: TelegramClient, db: Database, account_id: int):
        self.client = client
        self.db = db
        self.account_id = account_id
        self.junk_classifier = JunkChatClassifier(db=db)

        # Прогресс текущей фильтрации
        self._progress: Dict = {
            "running": False,
            "processed": 0,
            "total": 0,
            "passed": 0,
            "rejected": 0,
            "errors": 0,
        }

        # Результаты последней фильтрации
        self._results: List[Dict] = []

    def _log(self, message: str, level: str = "info"):
        """Логирует сообщение"""
        if self.db and self.account_id:
            try:
                self.db.add_log(self.account_id, level, f"[ChannelFilter] {message}")
            except Exception:
                pass
        print(f"[ChannelFilter] {message}")

    def _is_globally_excluded(self, channel: str) -> bool:
        try:
            if self.db and hasattr(self.db, "is_channel_globally_excluded"):
                return bool(self.db.is_channel_globally_excluded(channel))
        except Exception:
            return False
        return False

    def _structurally_exclude(self, channel: str, reason: str = "no_linked_chat", evidence=None):
        try:
            if hasattr(self.db, "exclude_channel_globally"):
                self.db.exclude_channel_globally(
                    channel,
                    reason,
                    evidence=evidence,
                    source_module="channel_filter",
                )
            if hasattr(self.db, "update_channel_comments_status"):
                try:
                    self.db.update_channel_comments_status(
                        channel,
                        has_open_comments=False,
                        structural=True,
                        reason=reason,
                        evidence=evidence,
                        source_module="channel_filter",
                    )
                except TypeError:
                    self.db.update_channel_comments_status(channel, has_open_comments=False)
        except Exception as e:
            self._log(f"structural exclude {channel}: {e}", "warning")

    async def get_filter_progress(self) -> Dict:
        """Возвращает текущий прогресс фильтрации"""
        return dict(self._progress)

    def get_results(self) -> List[Dict]:
        """Возвращает результаты последней фильтрации"""
        return list(self._results)

    async def import_and_filter(self, text_content: str, criteria: dict = None) -> Dict:
        """
        Парсит текстовый контент (одна строка = один канал/ссылка),
        нормализует через Config.normalize_channel() и прогоняет через bulk_filter.
        """
        lines = text_content.strip().splitlines()
        channels = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                normalized = Config.normalize_channel(line)
                if normalized:
                    channels.append(normalized)

        # Убираем дубликаты с сохранением порядка
        seen = set()
        unique_channels = []
        for ch in channels:
            if ch not in seen:
                seen.add(ch)
                unique_channels.append(ch)

        self._log(f"Импортировано {len(unique_channels)} каналов из текста")
        return await self.bulk_filter(unique_channels, criteria)

    async def filter_existing_db(self, criteria: dict = None) -> Dict:
        """
        Фильтрует все каналы, которые уже есть в БД (таблица found_channels).
        Помечает не прошедшие фильтрацию.
        """
        db_channels = self.db.get_found_channels(limit=5000, only_open_comments=False)
        channels = [ch['channel'] for ch in db_channels]

        self._log(f"Фильтрация существующей БД: {len(channels)} каналов")
        return await self.bulk_filter(channels, criteria)

    async def bulk_filter(self, channels: list, criteria: dict = None) -> Dict:
        """
        Массовая проверка списка каналов по критериям.

        Критерии:
        - min_subscribers: минимум подписчиков (дефолт 1000)
        - min_avg_views: минимум средних просмотров на пост (дефолт 300)
        - max_days_since_last_post: максимум дней с последнего поста (дефолт 7)
        - require_open_comments: требовать открытые комментарии (дефолт True)
        - junk_filter: проверять через JunkChatClassifier (дефолт True)
        - min_posts_per_week: минимум постов в неделю (дефолт 2)

        Возвращает статистику и детальные результаты.
        """
        if criteria is None:
            criteria = {}

        min_subscribers = criteria.get(
            'min_subscribers',
            int(self.db.get_setting('filter_min_subscribers', Config.FILTER_MIN_SUBSCRIBERS))
        )
        min_avg_views = criteria.get(
            'min_avg_views',
            int(self.db.get_setting('filter_min_avg_views', Config.FILTER_MIN_AVG_VIEWS))
        )
        max_days_since_last_post = criteria.get(
            'max_days_since_last_post',
            int(self.db.get_setting('filter_max_days_since_post', Config.FILTER_MAX_DAYS_SINCE_POST))
        )
        require_open_comments = criteria.get(
            'require_open_comments',
            bool(self.db.get_setting('filter_require_open_comments', Config.FILTER_REQUIRE_OPEN_COMMENTS))
        )
        junk_filter = criteria.get(
            'junk_filter', True
        )
        min_posts_per_week = criteria.get(
            'min_posts_per_week',
            int(self.db.get_setting('filter_min_posts_per_week', Config.FILTER_MIN_POSTS_PER_WEEK))
        )

        batch_size = int(self.db.get_setting('filter_batch_size', Config.FILTER_BATCH_SIZE))
        delay_min = int(self.db.get_setting('filter_batch_delay_min', Config.FILTER_BATCH_DELAY_MIN))
        delay_max = int(self.db.get_setting('filter_batch_delay_max', Config.FILTER_BATCH_DELAY_MAX))

        total = len(channels)
        self._progress = {
            "running": True,
            "processed": 0,
            "total": total,
            "passed": 0,
            "rejected": 0,
            "errors": 0,
        }
        self._results = []

        self._log(f"Начинаю фильтрацию {total} каналов (min_subs={min_subscribers}, "
                  f"min_views={min_avg_views}, max_days={max_days_since_last_post})")

        for i, channel in enumerate(channels):
            if not self._progress["running"]:
                self._log("Фильтрация остановлена")
                break

            if self._is_globally_excluded(channel):
                result = {
                    "channel": channel,
                    "status": "rejected",
                    "reason": "globally_excluded",
                    "stats": {},
                }
                self._results.append(result)
                self._progress['rejected'] += 1
                self._progress['processed'] = i + 1
                continue

            result = await self._check_channel(
                channel,
                min_subscribers=min_subscribers,
                min_avg_views=min_avg_views,
                max_days_since_last_post=max_days_since_last_post,
                require_open_comments=require_open_comments,
                junk_filter=junk_filter,
                min_posts_per_week=min_posts_per_week,
            )
            self._results.append(result)

            if result['status'] == 'passed':
                self._progress['passed'] += 1
            elif result['status'] == 'rejected':
                self._progress['rejected'] += 1
            else:
                self._progress['errors'] += 1

            self._progress['processed'] = i + 1

            # Антибан: задержка между проверками
            if (i + 1) % batch_size == 0:
                pause = random.randint(delay_min * 2, delay_max * 2)
                self._log(f"Пауза батча ({i + 1}/{total}): {pause} сек")
                await asyncio.sleep(pause)
            else:
                await asyncio.sleep(random.randint(delay_min, delay_max))

        self._progress['running'] = False

        summary = {
            "total": total,
            "passed": self._progress['passed'],
            "rejected": self._progress['rejected'],
            "errors": self._progress['errors'],
            "results": self._results,
        }

        self._log(
            f"Фильтрация завершена: {summary['passed']} прошли / "
            f"{summary['rejected']} отсеяны / {summary['errors']} ошибок"
        )

        return summary

    async def _check_channel(
        self,
        channel: str,
        min_subscribers: int,
        min_avg_views: int,
        max_days_since_last_post: int,
        require_open_comments: bool,
        junk_filter: bool,
        min_posts_per_week: int,
    ) -> Dict:
        """Проверяет один канал по всем критериям"""
        result = {
            "channel": channel,
            "status": "error",
            "reason": "",
            "stats": {},
        }

        try:
            # 1. Получаем entity канала
            try:
                entity = await self.client.get_entity(channel)
            except (UsernameNotOccupiedError, ValueError):
                result['status'] = 'rejected'
                result['reason'] = 'Канал не существует или недоступен'
                return result
            except ChannelPrivateError:
                # Account/access-local failure — do NOT structural-exclude globally
                result['status'] = 'rejected'
                result['reason'] = 'Канал приватный, нет доступа'
                result['stats']['local_only'] = True
                return result

            # Проверяем что это канал
            if not getattr(entity, 'broadcast', False):
                result['status'] = 'rejected'
                result['reason'] = 'Не является каналом (группа/чат)'
                return result

            # 2. Получаем полную информацию о канале
            try:
                full_channel = await self.client(GetFullChannelRequest(entity))
            except Exception as e:
                result['status'] = 'error'
                result['reason'] = f'Не удалось получить информацию: {e}'
                return result

            subscribers = full_channel.full_chat.participants_count or 0
            linked_chat_id = full_channel.full_chat.linked_chat_id
            about = full_channel.full_chat.about or ""
            title = getattr(entity, 'title', '') or ""
            username = getattr(entity, 'username', '') or ""

            result['stats']['subscribers'] = subscribers
            result['stats']['title'] = title

            # 3. Проверка минимума подписчиков
            if subscribers < min_subscribers:
                result['status'] = 'rejected'
                result['reason'] = f'Мало подписчиков: {subscribers} < {min_subscribers}'
                return result

            # 4. Проверка открытых комментариев — structural proof via linked_chat_id
            if require_open_comments and not linked_chat_id:
                # Successful GetFullChannel with no linked chat => structural global exclusion
                self._structurally_exclude(
                    channel,
                    reason="no_linked_chat",
                    evidence={"linked_chat_id": None},
                )
                result['status'] = 'rejected'
                result['reason'] = 'no_linked_chat'
                result['stats']['structural_exclude'] = True
                return result

            # 5. Получаем последние 10 постов
            messages = await self.client.get_messages(entity, limit=10)
            if not messages:
                result['status'] = 'rejected'
                result['reason'] = 'Нет постов в канале'
                return result

            # 6. Проверка возраста последнего поста
            now = datetime.now(timezone.utc)
            last_post = messages[0]
            if last_post.date:
                last_post_date = last_post.date
                if last_post_date.tzinfo is None:
                    last_post_date = last_post_date.replace(tzinfo=timezone.utc)
                days_since_last = (now - last_post_date).days
                result['stats']['days_since_last_post'] = days_since_last

                if days_since_last > max_days_since_last_post:
                    result['status'] = 'rejected'
                    result['reason'] = f'Мертвый канал: последний пост {days_since_last} дней назад'
                    return result

            # 7. Проверка средних просмотров
            views_list = [m.views for m in messages if m.views is not None]
            if views_list:
                avg_views = sum(views_list) / len(views_list)
                result['stats']['avg_views'] = int(avg_views)

                if avg_views < min_avg_views:
                    result['status'] = 'rejected'
                    result['reason'] = f'Мало просмотров: {int(avg_views)} < {min_avg_views}'
                    return result

            # 8. Проверка частоты постинга (постов в неделю)
            if len(messages) >= 2:
                first_post_date = messages[-1].date
                last_post_date_check = messages[0].date
                if first_post_date and last_post_date_check:
                    if first_post_date.tzinfo is None:
                        first_post_date = first_post_date.replace(tzinfo=timezone.utc)
                    if last_post_date_check.tzinfo is None:
                        last_post_date_check = last_post_date_check.replace(tzinfo=timezone.utc)

                    span_days = (last_post_date_check - first_post_date).days
                    if span_days > 0:
                        posts_per_week = (len(messages) / span_days) * 7
                        result['stats']['posts_per_week'] = round(posts_per_week, 1)

                        if posts_per_week < min_posts_per_week:
                            result['status'] = 'rejected'
                            result['reason'] = (
                                f'Редкие публикации: {posts_per_week:.1f} постов/нед < {min_posts_per_week}'
                            )
                            return result

            # 9. Junk filter через JunkChatClassifier
            if junk_filter:
                verdict = self.junk_classifier.heuristic_check(
                    title=title,
                    about=about,
                    members_count=subscribers,
                    username=username,
                )
                if verdict is not None and verdict.is_junk:
                    result['status'] = 'rejected'
                    result['reason'] = f'Мусорный канал: {verdict.reason}'
                    return result

            # Все проверки пройдены
            result['status'] = 'passed'
            result['reason'] = 'OK'
            return result

        except FloodWaitError as e:
            self._log(f"FloodWait: ожидание {e.seconds} секунд", "warning")
            await asyncio.sleep(e.seconds + 5)
            result['status'] = 'error'
            result['reason'] = f'FloodWait: {e.seconds} сек'
            return result
        except PeerFloodError:
            self._log("PeerFloodError: слишком много запросов", "warning")
            await asyncio.sleep(60)
            result['status'] = 'error'
            result['reason'] = 'PeerFloodError'
            return result
        except Exception as e:
            result['status'] = 'error'
            result['reason'] = f'Ошибка: {str(e)[:100]}'
            return result

    async def apply_results(self, remove_rejected: bool = True) -> Dict:
        """
        Применяет результаты фильтрации: удаляет rejected каналы из БД.

        Returns:
            {"removed": int, "kept": int}
        """
        removed = 0
        kept = 0

        for item in self._results:
            if item['status'] == 'rejected' and remove_rejected:
                try:
                    self.db.delete_found_channel(item['channel'], force=True)
                    removed += 1
                except Exception:
                    pass
            elif item['status'] == 'passed':
                kept += 1

        self._log(f"Применены результаты: удалено {removed}, оставлено {kept}")
        return {"removed": removed, "kept": kept}

    def stop(self):
        """Останавливает текущую фильтрацию"""
        self._progress['running'] = False
