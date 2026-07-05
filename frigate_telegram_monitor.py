#!/usr/bin/env python3
"""
Frigate Telegram Monitor - Мониторинг событий Frigate и отправка в Telegram
Поддерживает несколько групп камер с разными Telegram чатами
"""

import asyncio
import json
import logging
import os
import sys
import time

# --version: печатаем версию и выходим (до тяжёлых импортов и без config.py)
if "--version" in sys.argv:
    _d = os.path.dirname(os.path.abspath(__file__))
    try:
        print("frigate-notify-alert", open(os.path.join(_d, "VERSION")).read().strip())
    except OSError:
        print("frigate-notify-alert unknown")
    raise SystemExit(0)

from typing import Dict, Any, List
import aiohttp
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
import paho.mqtt.client as mqtt

# Импортируем конфигурацию
try:
    from config import *
except ModuleNotFoundError as _e:
    if getattr(_e, "name", "") == "config":
        print("config.py not found. Copy the example and fill it in:")
        print("   cp config.example.py config.py")
        raise SystemExit(1)
    raise

# Файл общего состояния паузы (mute). Пишет его mute_controller, читают мониторы.
# Путь можно переопределить в config.py (MUTE_STATE_FILE), иначе — рядом со скриптом.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MUTE_STATE_FILE = globals().get("MUTE_STATE_FILE") or os.path.join(_SCRIPT_DIR, "mute_state.json")

class FrigateTelegramMonitor:
    def __init__(self, group_id: str):
        """Инициализация монитора для указанной группы"""
        self.group_id = group_id
        self.group_config = GROUPS[group_id]

        # Момент запуска: события, завершившиеся ДО него, не шлём (иначе при старте
        # улетает вся история из /api/events, т.к. список обработанных ещё пуст).
        self.startup_ts = time.time()

        # Настройка логирования
        self.logger = self._setup_logging()
        
        # Статистика
        self.stats = {
            "start_time": time.time(),
            "events_processed": 0,
            "telegram_sent": 0,
            "errors": 0
        }
        
        # Telegram бот (увеличенные таймауты + прокси для обхода блокировок)
        request_kw: dict = {
            "connect_timeout": 30,
            "read_timeout": 120,
            "write_timeout": 90,
            "media_write_timeout": 180,
        }
        if TELEGRAM_PROXY_URL:
            request_kw["proxy"] = TELEGRAM_PROXY_URL
            self.logger.info(f"📡 Telegram через прокси: {TELEGRAM_PROXY_URL.split('@')[1]}")
        request = HTTPXRequest(**request_kw)
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
        
        # Списки камер и объектов
        self.cameras = self.group_config["cameras"]
        self.objects = ["person", "car", "truck", "bus", "motorcycle", "bicycle"]

        # Фильтр по зонам Frigate (необязательный).
        # Пусто/нет ключа "zones" в группе = слать по всей камере (как раньше).
        # Если задан список зон — уведомление уйдёт, только если объект заходил
        # хотя бы в одну из этих зон (поле события Frigate entered_zones).
        self.zones = self.group_config.get("zones") or []
        if self.zones:
            self.logger.info(f"🧭 Фильтр по зонам включён: {', '.join(self.zones)}")
        
        # ID обработанных событий (чтобы не дублировать)
        # Ограничиваем размер, чтобы не накапливать слишком много
        self.processed_events = set()
        self.max_processed_events = 1000  # Максимум 1000 ID в памяти
        
        # События для повторной проверки (если медиа еще не готово)
        # {event_id: (event_data, retry_count)}
        self.retry_events = {}
        self.max_retries = 10  # Максимум 10 попыток (30 секунд)
        
        # MQTT клиент для получения событий в реальном времени
        self.mqtt_client = None
        self.mqtt_connected = False
        self.event_loop = None
        
        # Очередь событий из MQTT для обработки
        self.mqtt_event_queue = None
        
        self.logger.info(f"🚀 Инициализация монитора для группы: {self.group_config['name']}")
    
    def _setup_logging(self):
        """Настройка логирования"""
        logger = logging.getLogger(f"frigate_monitor_{self.group_id}")
        logger.setLevel(logging.INFO)
        
        # Форматтер с эмодзи и московским временем
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%d.%m.%Y %H:%M:%S'
        )
        
        # Консольный вывод
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # Файловый вывод
        file_handler = logging.FileHandler(f"frigate_monitor_{self.group_id}.log")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        return logger

    def _zone_ok(self, event: Dict[str, Any]) -> bool:
        """Проверка фильтра по зонам.

        True — если фильтр не задан (self.zones пуст) ИЛИ объект заходил
        хотя бы в одну из нужных зон.

        Frigate отдаёт список зон под разными ключами: в MQTT-событии это
        entered_zones, в HTTP /api/events — zones. Берём объединение обоих,
        чтобы фильтр одинаково работал в обоих путях обработки.
        """
        if not self.zones:
            return True
        entered = set(event.get("entered_zones") or []) | set(event.get("zones") or [])
        return bool(entered & set(self.zones))

    def _is_muted(self) -> bool:
        """Проверяет общий файл паузы: стоит ли сейчас пауза для этой группы.

        Файлом управляет mute_controller (кнопки в Telegram). Формат:
        {"group1": {"muted_until": <epoch_sec>}, ...}. Нет файла/ключа = не заглушено.
        """
        try:
            with open(MUTE_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        until = (state.get(self.group_id) or {}).get("muted_until", 0)
        return bool(until and until > time.time())

    async def _check_frigate_events(self):
        """Периодическая проверка событий через Frigate API"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{FRIGATE_URL}/api/events") as response:
                    if response.status == 200:
                        events = await response.json()
                        
                        # Логируем общее количество событий
                        total_events = len(events)
                        our_camera_events = [e for e in events if e.get("camera") in self.cameras]
                        self.logger.info(f"📊 Всего событий в API: {total_events}, для наших камер: {len(our_camera_events)}")
                        
                        # Логируем детали ВСЕХ событий для наших камер
                        if our_camera_events:
                            self.logger.info(f"🔍 Все события для наших камер ({len(our_camera_events)}):")
                            for e in our_camera_events:
                                event_id = e.get('id')
                                is_processed = event_id in self.processed_events
                                status = "✅ обработано" if is_processed else "⏳ новое"
                                self.logger.info(
                                    f"  - {event_id}: {e.get('label')}, "
                                    f"end_time={e.get('end_time')}, "
                                    f"has_snapshot={e.get('has_snapshot')}, "
                                    f"has_clip={e.get('has_clip')}, "
                                    f"{status}"
                                )
                        
                        # Счетчики для статистики
                        skipped_no_end_time = 0
                        skipped_wrong_camera = 0
                        skipped_wrong_object = 0
                        skipped_wrong_zone = 0
                        skipped_old = 0
                        skipped_no_snapshot = 0
                        skipped_no_clip = 0
                        already_processed = 0
                        new_events = 0
                        
                        # Сортируем события по времени окончания (новые первыми)
                        events_sorted = sorted(events, key=lambda x: x.get("end_time", 0) or 0, reverse=True)
                        
                        for event in events_sorted:
                            event_id = event.get("id")
                            
                            camera = event.get("camera")
                            object_type = event.get("label")
                            end_time = event.get("end_time")
                            has_snapshot = event.get("has_snapshot", False)
                            has_clip = event.get("has_clip", False)
                            
                            # Проверяем условия обработки
                            if not end_time:
                                skipped_no_end_time += 1
                                # Логируем только для наших камер
                                if camera in self.cameras:
                                    self.logger.debug(
                                        f"⏳ Событие {event_id} ({object_type} на {camera}): "
                                        f"еще не завершено (нет end_time)"
                                    )
                                continue  # Пропускаем незавершенные события
                            
                            # Пропускаем события, которые уже обработаны (по ID)
                            if event_id in self.processed_events:
                                already_processed += 1
                                continue
                            
                            if camera not in self.cameras:
                                skipped_wrong_camera += 1
                                continue  # Пропускаем события с других камер
                            
                            if object_type not in self.objects:
                                skipped_wrong_object += 1
                                self.logger.debug(
                                    f"⏭️ Пропуск события {event_id} ({object_type} на {camera}): "
                                    f"объект '{object_type}' не в списке отслеживаемых"
                                )
                                continue

                            # Событие завершилось ДО запуска скрипта — это «история» из API,
                            # не шлём (иначе при каждом старте улетает бэклог за последний час).
                            # Помечаем обработанным, чтобы больше не рассматривать.
                            if end_time < self.startup_ts:
                                self.processed_events.add(event_id)
                                skipped_old += 1
                                self.logger.debug(
                                    f"⏭️ Пропуск старого события {event_id} ({object_type} на {camera}): "
                                    f"завершилось до запуска (end_time={end_time} < старт={self.startup_ts:.0f})"
                                )
                                continue

                            # Фильтр по зонам: объект не заходил в нужную зону — пропускаем
                            if not self._zone_ok(event):
                                skipped_wrong_zone += 1
                                self.logger.debug(
                                    f"⏭️ Пропуск события {event_id} ({object_type} на {camera}): "
                                    f"вне нужных зон {self.zones} "
                                    f"(был в {event.get('entered_zones') or event.get('zones') or []})"
                                )
                                continue

                            # Если медиа еще не готово, добавляем в список для повторной проверки
                            if not has_snapshot or not has_clip:
                                if event_id not in self.retry_events:
                                    self.retry_events[event_id] = (event, 0)
                                    self.logger.info(
                                        f"⏳ Событие {event_id} ({object_type} на {camera}): "
                                        f"медиа еще не готово (snapshot={has_snapshot}, clip={has_clip}), "
                                        f"добавлено в очередь повторной проверки"
                                    )
                                else:
                                    # Увеличиваем счетчик попыток
                                    old_event, retry_count = self.retry_events[event_id]
                                    self.retry_events[event_id] = (event, retry_count + 1)
                                    
                                    if retry_count + 1 >= self.max_retries:
                                        self.logger.warning(
                                            f"❌ Событие {event_id} ({object_type} на {camera}): "
                                            f"медиа не появилось после {self.max_retries} попыток, пропускаем"
                                        )
                                        del self.retry_events[event_id]
                                        if not has_snapshot:
                                            skipped_no_snapshot += 1
                                        if not has_clip:
                                            skipped_no_clip += 1
                                continue
                            
                            # Если событие было в очереди повторной проверки, удаляем его
                            if event_id in self.retry_events:
                                old_event, retry_count = self.retry_events[event_id]
                                del self.retry_events[event_id]
                                self.logger.info(
                                    f"✅ Событие {event_id} ({object_type} на {camera}): "
                                    f"медиа появилось после {retry_count + 1} попыток"
                                )
                            
                            # Обрабатываем событие (требуем и фото, и видео)
                            self.logger.info(f"🎯 Обнаружен объект: {object_type} на камере {camera} (snapshot: {has_snapshot}, clip: {has_clip}, end_time: {end_time})")
                            self.logger.info(f"🔄 Обработка завершенного события {event_id} для камеры {camera}")
                            
                            # Обрабатываем событие
                            await self._process_frigate_event(event)
                            
                            # Помечаем как обработанное
                            self.processed_events.add(event_id)
                            
                            # Ограничиваем размер set, удаляя старые записи
                            if len(self.processed_events) > self.max_processed_events:
                                # Удаляем самые старые (первые в отсортированном списке)
                                old_events = sorted(self.processed_events)[:len(self.processed_events) - self.max_processed_events + 100]
                                for old_id in old_events:
                                    self.processed_events.discard(old_id)
                            
                            new_events += 1
                        
                        # Проверяем события из retry_events, которых нет в общем списке API
                        # но могут быть доступны по прямому URL
                        event_ids_in_api = {e.get("id") for e in events}
                        retry_events_to_check = []
                        retry_events_to_remove = []
                        current_time = time.time()
                        
                        async with aiohttp.ClientSession() as session:
                            for retry_event_id, (retry_event, retry_count) in list(self.retry_events.items()):
                                # Если события нет в общем списке API, проверяем по прямому URL
                                if retry_event_id not in event_ids_in_api:
                                    try:
                                        async with session.get(f"{FRIGATE_URL}/api/events/{retry_event_id}", timeout=5) as response:
                                            if response.status == 200:
                                                # Событие найдено по прямому URL, обновляем данные
                                                updated_event = await response.json()
                                                has_snapshot = updated_event.get("has_snapshot", False)
                                                has_clip = updated_event.get("has_clip", False)
                                                
                                                if has_snapshot and has_clip:
                                                    self.logger.info(
                                                        f"✅ Событие {retry_event_id} найдено по прямому URL и готово к обработке "
                                                        f"(snapshot: {has_snapshot}, clip: {has_clip}, попытка {retry_count + 1})"
                                                    )
                                                    await self._process_frigate_event(updated_event)
                                                    del self.retry_events[retry_event_id]
                                                else:
                                                    # Обновляем событие в очереди
                                                    self.retry_events[retry_event_id] = (updated_event, retry_count + 1)
                                                    if retry_count + 1 >= self.max_retries:
                                                        self.logger.warning(
                                                            f"❌ Событие {retry_event_id}: медиа не появилось после {self.max_retries} попыток"
                                                        )
                                                        retry_events_to_remove.append(retry_event_id)
                                            elif response.status == 404:
                                                # Событие удалено из API, проверяем время
                                                retry_end_time = retry_event.get("end_time", 0)
                                                if retry_end_time > 0 and (current_time - retry_end_time) > 300:  # 5 минут
                                                    retry_events_to_remove.append(retry_event_id)
                                            else:
                                                # Другая ошибка, увеличиваем счетчик
                                                self.retry_events[retry_event_id] = (retry_event, retry_count + 1)
                                                if retry_count + 1 >= self.max_retries:
                                                    retry_events_to_remove.append(retry_event_id)
                                    except Exception as e:
                                        self.logger.debug(f"⚠️ Ошибка проверки события {retry_event_id} по прямому URL: {e}")
                                        # Увеличиваем счетчик при ошибке
                                        self.retry_events[retry_event_id] = (retry_event, retry_count + 1)
                                        if retry_count + 1 >= self.max_retries:
                                            retry_events_to_remove.append(retry_event_id)
                                else:
                                    # Событие есть в общем списке, оно будет обработано выше
                                    pass
                        
                        # Удаляем старые события
                        for retry_event_id in retry_events_to_remove:
                            retry_event, retry_count = self.retry_events[retry_event_id]
                            self.logger.warning(
                                f"🗑️ Удаление события {retry_event_id} из очереди повторной проверки: "
                                f"событие удалено из API или превышен лимит попыток (было {retry_count} попыток)"
                            )
                            del self.retry_events[retry_event_id]
                        
                        # Логируем статистику
                        if new_events > 0 or skipped_no_end_time > 0 or skipped_wrong_camera > 0 or skipped_wrong_object > 0 or skipped_wrong_zone > 0 or skipped_old > 0 or skipped_no_snapshot > 0 or skipped_no_clip > 0 or already_processed > 0 or len(self.retry_events) > 0:
                            self.logger.info(
                                f"📊 Статистика: "
                                f"новых обработано={new_events}, "
                                f"уже обработано={already_processed}, "
                                f"нет end_time={skipped_no_end_time}, "
                                f"другая камера={skipped_wrong_camera}, "
                                f"другой объект={skipped_wrong_object}, "
                                f"другая зона={skipped_wrong_zone}, "
                                f"старое (до старта)={skipped_old}, "
                                f"нет snapshot={skipped_no_snapshot}, "
                                f"нет clip={skipped_no_clip}, "
                                f"в памяти ID={len(self.processed_events)}, "
                                f"в очереди повторной проверки={len(self.retry_events)}"
                            )
                                
        except Exception as e:
            self.logger.error(f"❌ Ошибка проверки событий Frigate: {e}")
    
    async def _process_frigate_event(self, event: Dict[str, Any]):
        """Обработка события из Frigate API"""
        try:
            event_id = event.get("id")
            camera = event.get("camera")
            object_type = event.get("label")

            # Пауза уведомлений (кнопки в Telegram): если стоит — тихо пропускаем
            if self._is_muted():
                self.logger.info(
                    f"🔕 Пауза активна для {self.group_id} — событие {event_id} "
                    f"({object_type} на {camera}) не отправляем"
                )
                return

            # Формируем URL для медиа
            photo_url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg?crop=1"
            video_url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"
            
            self.logger.info(f"🔗 Snapshot URL: {photo_url}")
            self.logger.info(f"🔗 Clip URL: {video_url}")
            
            # Отправляем медиа (требуем и фото, и видео)
            success = await self._send_telegram_media_group_with_retry(photo_url, video_url, event_id)
            
            if success:
                self.logger.info(f"✅ Медиа успешно отправлены для события {event_id}")
                self.stats["telegram_sent"] += 1
            else:
                self.logger.error(f"❌ Не удалось отправить медиа для события {event_id}")
                self.stats["errors"] += 1
            
            self.stats["events_processed"] += 1
            
        except Exception as e:
            self.logger.error(f"❌ Ошибка обработки события: {e}")
            self.stats["errors"] += 1
    
    async def _send_telegram_media_group_with_retry(self, photo_url: str, video_url: str, event_id: str) -> bool:
        """Отправка медиа группы с повторными попытками загрузки"""
        for attempt in range(15):  # 15 попыток по 3 секунды = 45 секунд
            try:
                # Проверяем доступность медиа
                has_photo = False
                has_video = False
                
                async with aiohttp.ClientSession() as session:
                    # Проверяем фото
                    try:
                        self.logger.info(f"🔍 Попытка {attempt + 1}: проверка фото {photo_url}")
                        async with session.get(photo_url, timeout=10) as response:
                            self.logger.info(f"📸 HTTP ответ фото: {response.status}")
                            if response.status == 200:
                                photo_data = await response.read()
                                self.logger.info(f"📸 Размер фото: {len(photo_data)} байт")
                                if len(photo_data) > 1000:  # Минимальный размер файла
                                    has_photo = True
                                    self.logger.info(f"📸 Фото загружено успешно")
                                else:
                                    self.logger.warning(f"📸 Фото слишком маленькое: {len(photo_data)} байт")
                            else:
                                self.logger.warning(f"📸 Фото недоступно: HTTP {response.status}")
                    except Exception as e:
                        self.logger.error(f"📸 Ошибка загрузки фото (попытка {attempt + 1}): {e}")
                    
                    # Проверяем видео
                    try:
                        self.logger.info(f"🔍 Попытка {attempt + 1}: проверка видео {video_url}")
                        async with session.get(video_url, timeout=10) as response:
                            self.logger.info(f"🎥 HTTP ответ видео: {response.status}")
                            if response.status == 200:
                                video_data = await response.read()
                                self.logger.info(f"🎥 Размер видео: {len(video_data)} байт")
                                if len(video_data) > 1000:  # Минимальный размер файла
                                    has_video = True
                                    self.logger.info(f"🎥 Видео загружено успешно")
                                else:
                                    self.logger.warning(f"🎥 Видео слишком маленькое: {len(video_data)} байт")
                            else:
                                self.logger.warning(f"🎥 Видео недоступно: HTTP {response.status}")
                    except Exception as e:
                        self.logger.error(f"🎥 Ошибка загрузки видео (попытка {attempt + 1}): {e}")
                
                # Если оба файла доступны, отправляем
                if has_photo and has_video:
                    self.logger.info(f"📸 Найден snapshot и clip для {event_id} (попытка {attempt + 1})")
                    return await self._send_telegram_media_group(photo_url, video_url)
                
                # Ждем перед следующей попыткой
                if attempt < 14:
                    await asyncio.sleep(3)
                
            except Exception as e:
                self.logger.error(f"❌ Ошибка при попытке {attempt + 1}: {e}")
                if attempt < 14:
                    await asyncio.sleep(3)
        
        self.logger.error(f"❌ Медиа не появились для {event_id} после 15 попыток")
        return False
    
    async def _send_telegram_media_group(self, photo_url: str, video_url: str) -> bool:
        """Отправка медиа группы в Telegram"""
        try:
            # Загружаем медиа файлы локально
            photo_data = None
            video_data = None
            
            async with aiohttp.ClientSession() as session:
                # Загружаем фото
                async with session.get(photo_url, timeout=aiohttp.ClientTimeout(total=45)) as response:
                    if response.status == 200:
                        photo_data = await response.read()
                        self.logger.info(f"📸 Фото загружено: {len(photo_data)} байт")
                    else:
                        self.logger.error(f"❌ Ошибка загрузки фото: HTTP {response.status}")
                        return False
                
                # Загружаем видео
                async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=90)) as response:
                    if response.status == 200:
                        video_data = await response.read()
                        self.logger.info(f"🎥 Видео загружено: {len(video_data)} байт")
                    else:
                        self.logger.error(f"❌ Ошибка загрузки видео: HTTP {response.status}")
                        return False
            
            # Создаем медиа группу с загруженными данными
            media_group = [
                InputMediaPhoto(media=photo_data),
                InputMediaVideo(media=video_data)
            ]
            
            # Отправляем беззвучно
            await self.bot.send_media_group(
                chat_id=self.group_config["telegram_chat_id"],
                media=media_group,
                disable_notification=True
            )
            
            self.logger.info("✅ Медиа группа отправлена успешно")
            return True
            
        except TelegramError as e:
            self.logger.error(f"❌ Ошибка Telegram: {e}")
            return False
        except Exception as e:
            self.logger.error(f"❌ Ошибка отправки медиа: {e}")
            return False
    
    def _start_stats_timer(self):
        """Запуск таймера статистики"""
        def stats_timer():
            while True:
                time.sleep(60)  # Каждую минуту
                uptime = int(time.time() - self.stats["start_time"])
                self.logger.info(
                    f"📊 Статистика ({self.group_id}): "
                    f"Время работы: {uptime} сек, "
                    f"Событий обработано: {self.stats['events_processed']}, "
                    f"Отправлено в Telegram: {self.stats['telegram_sent']}, "
                    f"Ошибок: {self.stats['errors']}"
                )
        
        import threading
        stats_thread = threading.Thread(target=stats_timer, daemon=True)
        stats_thread.start()
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """Обработчик подключения к MQTT"""
        if rc == 0:
            self.mqtt_connected = True
            self.logger.info("✅ Подключено к MQTT брокеру")
            # Подписываемся на все события Frigate
            client.subscribe(f"{MQTT_TOPIC_PREFIX}/events")
            self.logger.info(f"📡 Подписка на топик: {MQTT_TOPIC_PREFIX}/events")
        else:
            self.mqtt_connected = False
            self.logger.error(f"❌ Ошибка подключения к MQTT: {rc}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """Обработчик отключения от MQTT"""
        self.mqtt_connected = False
        if rc != 0:
            self.logger.warning(f"⚠️ Неожиданное отключение от MQTT: {rc}")
        else:
            self.logger.info("🔌 Отключено от MQTT брокера")
    
    def _on_mqtt_message(self, client, userdata, msg):
        """Обработчик сообщений MQTT"""
        try:
            payload = json.loads(msg.payload.decode())
            event_type = payload.get("type")
            
            # Обрабатываем завершенные события (end) и обновления с end_time (update)
            event_data = None
            if event_type == "end":
                event_data = payload.get("after", {})
            elif event_type == "update":
                # Обрабатываем update только если есть end_time (событие завершено)
                event_data = payload.get("after", {})
                if not event_data.get("end_time"):
                    return  # Событие еще не завершено
            
            if event_data:
                camera = event_data.get("camera")
                object_type = event_data.get("label")
                event_id = event_data.get("id")
                end_time = event_data.get("end_time")
                
                # Проверяем, что это событие для наших камер и объектов
                if camera in self.cameras and object_type in self.objects and end_time:
                    # Фильтр по зонам: если объект не был в нужной зоне — игнорируем
                    if not self._zone_ok(event_data):
                        self.logger.debug(
                            f"⏭️ MQTT: пропуск {object_type} на {camera} (ID: {event_id}): "
                            f"вне нужных зон {self.zones} "
                            f"(был в {event_data.get('entered_zones') or []})"
                        )
                        return
                    self.logger.info(
                        f"📨 Получено событие из MQTT ({event_type}): {object_type} на {camera} "
                        f"(ID: {event_id}, end_time: {end_time})"
                    )
                    # Добавляем в очередь для обработки
                    if self.event_loop and self.mqtt_event_queue:
                        asyncio.run_coroutine_threadsafe(
                            self.mqtt_event_queue.put(event_data),
                            self.event_loop
                        )
        except Exception as e:
            self.logger.error(f"❌ Ошибка обработки MQTT сообщения: {e}")
    
    def _start_mqtt_client(self):
        """Запуск MQTT клиента в отдельном потоке"""
        def mqtt_thread():
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_message = self._on_mqtt_message
            
            try:
                self.mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
                self.mqtt_client.loop_forever()
            except Exception as e:
                self.logger.error(f"❌ Ошибка MQTT клиента: {e}")
        
        import threading
        thread = threading.Thread(target=mqtt_thread, daemon=True)
        thread.start()
        self.logger.info("🚀 MQTT клиент запущен в отдельном потоке")
    
    async def _process_mqtt_events(self):
        """Обработка событий из MQTT очереди"""
        while True:
            try:
                # Получаем событие из очереди (с таймаутом)
                event = await asyncio.wait_for(self.mqtt_event_queue.get(), timeout=1.0)
                
                event_id = event.get("id")
                camera = event.get("camera")
                object_type = event.get("label")
                
                # Проверяем, не обработано ли уже
                if event_id in self.processed_events:
                    self.logger.debug(f"⏭️ Событие {event_id} уже обработано, пропускаем")
                    continue
                
                # Помечаем как обрабатываемое сразу, чтобы не обработать дважды через API
                self.processed_events.add(event_id)
                
                self.logger.info(
                    f"📨 Получено событие из MQTT: {object_type} на {camera} (ID: {event_id})"
                )
                
                # Получаем полную информацию о событии из API (с повторными попытками)
                full_event = None
                for attempt in range(20):  # 20 попыток по 3 секунды = 60 секунд
                    async with aiohttp.ClientSession() as session:
                        try:
                            async with session.get(f"{FRIGATE_URL}/api/events/{event_id}", timeout=10) as response:
                                if response.status == 200:
                                    full_event = await response.json()
                                    self.logger.info(
                                        f"✅ Событие {event_id} получено из API (попытка {attempt + 1})"
                                    )
                                    break
                                elif response.status == 404:
                                    # Событие еще не появилось в API, ждем
                                    if attempt < 19:
                                        await asyncio.sleep(3)
                                    else:
                                        self.logger.warning(
                                            f"⚠️ Событие {event_id} не найдено в API после {attempt + 1} попыток"
                                        )
                                else:
                                    self.logger.warning(
                                        f"⚠️ HTTP {response.status} при получении события {event_id} (попытка {attempt + 1})"
                                    )
                                    if attempt < 19:
                                        await asyncio.sleep(3)
                        except Exception as e:
                            self.logger.warning(
                                f"⚠️ Ошибка при получении события {event_id} (попытка {attempt + 1}): {e}"
                            )
                            if attempt < 19:
                                await asyncio.sleep(3)
                
                if full_event:
                    has_snapshot = full_event.get("has_snapshot", False)
                    has_clip = full_event.get("has_clip", False)
                    
                    if has_snapshot and has_clip:
                        self.logger.info(
                            f"✅ Событие {event_id} готово к обработке "
                            f"(snapshot: {has_snapshot}, clip: {has_clip})"
                        )
                        await self._process_frigate_event(full_event)
                    else:
                        # Добавляем в очередь повторной проверки
                        self.retry_events[event_id] = (full_event, 0)
                        self.logger.info(
                            f"⏳ Событие {event_id} добавлено в очередь повторной проверки "
                            f"(snapshot: {has_snapshot}, clip: {has_clip})"
                        )
                else:
                    # Если событие не получено из API, добавляем в очередь повторной проверки с исходными данными
                    self.retry_events[event_id] = (event, 0)
                    self.logger.info(
                        f"⏳ Событие {event_id} добавлено в очередь повторной проверки "
                        f"(не удалось получить из API)"
                    )
            except asyncio.TimeoutError:
                # Таймаут - это нормально, продолжаем
                continue
            except Exception as e:
                self.logger.error(f"❌ Ошибка обработки MQTT события: {e}")
    
    async def start_monitoring(self):
        """Запуск мониторинга"""
        self.logger.info(f"🚀 Запуск мониторинга Frigate ({self.group_config['name']})")
        self.logger.info(f"📹 Камеры: {', '.join(self.cameras)}")
        self.logger.info(f"🎯 Отслеживаемые объекты: {', '.join(self.objects)}")
        
        # Сохраняем event loop и создаем очередь
        self.event_loop = asyncio.get_event_loop()
        self.mqtt_event_queue = asyncio.Queue()
        
        # Запускаем статистику
        self._start_stats_timer()
        
        # Запускаем MQTT клиент для получения событий в реальном времени
        self._start_mqtt_client()
        
        # Запускаем обработку событий из MQTT
        mqtt_task = asyncio.create_task(self._process_mqtt_events())
        
        # Основной цикл проверки событий (для обработки событий из очереди повторной проверки)
        try:
            while True:
                await self._check_frigate_events()
                await asyncio.sleep(3)  # Проверяем каждые 3 секунды
        except KeyboardInterrupt:
            self.logger.info("🛑 Получен сигнал завершения")
            mqtt_task.cancel()
        except Exception as e:
            self.logger.error(f"❌ Критическая ошибка: {e}")
            mqtt_task.cancel()

def main():
    """Главная функция"""
    if len(sys.argv) != 2:
        print("Использование: python frigate_telegram_monitor.py <group_id>")
        print("Доступные группы:", list(GROUPS.keys()))
        sys.exit(1)
    
    group_id = sys.argv[1]
    
    if group_id not in GROUPS:
        print(f"Ошибка: группа '{group_id}' не найдена")
        print("Доступные группы:", list(GROUPS.keys()))
        sys.exit(1)
    
    # Создаем и запускаем монитор
    monitor = FrigateTelegramMonitor(group_id)
    
    try:
        asyncio.run(monitor.start_monitoring())
    except KeyboardInterrupt:
        print("\n🛑 Завершение работы...")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()