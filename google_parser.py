import json
import os
import re
import time
import shutil
from datetime import datetime, timedelta

from appium import webdriver
from appium.options.common import AppiumOptions
import cv2
import numpy as np
from PIL import Image
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.by import By

MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

# Внутри карточки вертикальный зазор между текстами не бывает больше порога,
# между карточками — всегда больше.
CARD_GAP_PX = 80

# Служебные тексты, которые никогда не могут быть названием станции
SERVICE_WORDS = {
    "История", "Проходы", "Покупки", "Операции", "Операция", "Фильтры",
    "Проход", "Покупка", "Мед. карта", "Маршрут", "Кабинет", "Город", "Новости",
}

# Реальный размер окна в координатах UI (обновляется в main).
SCREEN_SIZE = (1308, 2880)


def parse_date_header(text: str, current_year: int) -> datetime | None:
    text = text.strip().lower()
    today = datetime.now()

    if "сегодня" in text:
        return today.replace(hour=0, minute=0, second=0, microsecond=0)
    if "вчера" in text:
        return (today - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    match = re.match(r"(\d+)\s+([а-я]+)", text)
    if match:
        month = MONTH_MAP.get(match.group(2))
        if month:
            return datetime(year=current_year, month=month, day=int(match.group(1)))
    return None


def is_date_text(text: str) -> bool:
    t = text.strip().lower()
    if "сегодня" in t or "вчера" in t:
        return True
    return any(month in t for month in MONTH_MAP.keys())


def parse_bounds(bounds_str: str):
    """'[182,835][872,891]' -> (182, 835, 872, 891). Без регулярных выражений,
    чтобы не зависеть от экранирования при копировании."""
    if not bounds_str:
        return None
    parts = bounds_str.replace("][", ",").strip("[]").split(",")
    if len(parts) == 4:
        try:
            return tuple(int(v) for v in parts)
        except ValueError:
            return None
    return None


def get_driver():
    print("[LOG] Инициализация драйвера Appium...")
    options = AppiumOptions()
    options.set_capability("platformName", "Android")
    options.set_capability("automationName", "UiAutomator2")
    options.set_capability("udid", "86R0226708007114")
    options.set_capability("adbExecPath", r"C:\Users\zakaz\Downloads\platform-tools\adb.exe")
    options.set_capability("appPackage", "ru.mosmetro.metro")
    options.set_capability("noReset", True)
    options.set_capability("dontStopAppOnReset", True)
    options.set_capability("uiautomator2ServerNewCommandTimeout", 90000)

    driver = webdriver.Remote("http://localhost:4723", options=options)
    print("[LOG] Драйвер успешно подключен к сессии.")
    return driver


def save_to_json(new_data: list, file_path: str = "history.json"):
    """Сохраняет уникальные записи. Если время уже есть, дубликат не пишется."""
    existing_data = []
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except json.JSONDecodeError:
            existing_data = []

    existing_timestamps = {item[0] for item in existing_data if len(item) >= 1}

    added_count = 0
    for dt_str, title, price in new_data:
        if dt_str not in existing_timestamps:
            existing_data.append([dt_str, title, price])
            existing_timestamps.add(dt_str)
            added_count += 1

    if added_count > 0:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=4)
        print(f"[LOG] Добавлено новых уникальных записей в JSON: {added_count} (Всего в базе: {len(existing_data)})")


def compare_images(img_path1, img_path2) -> float:
    try:
        img1 = cv2.imread(img_path1, cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(img_path2, cv2.IMREAD_GRAYSCALE)
        if img1 is None or img2 is None:
            return 0.0
        if img1.shape != img2.shape:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
        res = cv2.matchTemplate(img1, img2, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        return max_val
    except Exception as e:
        print(f"[LOG] Ошибка сравнения картинок: {e}")
        return 0.0


def drag_up_and_stop(driver, start_x, start_y, end_y):
    """Скроллинг с фиксацией пальца на 0.5с для полного гашения инерции списка."""
    print(f"[LOG] Выполнение свайпа: ({start_x}, {start_y}) -> ({start_x}, {end_y})...")
    actions = ActionChains(driver)
    pointer = PointerInput(interaction.POINTER_TOUCH, "finger")

    actions.w3c_actions.pointer_action.move_to_location(start_x, start_y)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.move_to_location(start_x, end_y)
    actions.w3c_actions.pointer_action.pause(0.5)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    print("[LOG] Экран зафиксирован. Ждем остановку анимации...")
    time.sleep(2.0)


def collect_screen_items(driver) -> list:
    """Собирает все текстовые элементы с их координатами (bounds)."""
    all_elements = driver.find_elements(By.XPATH, "//*[@text!='']")
    items = []
    for elem in all_elements:
        try:
            text = (elem.get_attribute("text") or "").strip()
            if not text:
                continue
            bounds = parse_bounds(elem.get_attribute("bounds"))
            if bounds:
                x1, y1, x2, y2 = bounds
            else:
                y1 = elem.location['y']
                y2, x1 = y1 + 50, 0
            items.append({"y1": y1, "y2": y2, "x1": x1, "text": text})
        except Exception:
            continue
    items.sort(key=lambda it: (it["y1"], it["x1"]))
    return items


def group_into_cards(items: list) -> list:
    """Группирует элементы в карточки по вертикальным зазорам.
    Плашка даты — отдельная группа-разделитель: элементы после неё
    никогда не приклеиваются к ней, а начинают новую карточку."""
    groups = []
    for it in items:
        if is_date_text(it["text"]):
            groups.append({"date_text": it["text"], "items": [], "max_y": it["y2"]})
            continue
        prev = groups[-1] if groups else None
        if prev and prev["date_text"] is None and it["y1"] - prev["max_y"] <= CARD_GAP_PX:
            prev["items"].append(it)
            prev["max_y"] = max(prev["max_y"], it["y2"])
        else:
            groups.append({"date_text": None, "items": [it], "max_y": it["y2"]})
    return groups


def parse_card_group(items: list, current_date_str: str) -> tuple | None:
    """Разбирает одну карточку. Роли полей определяются по содержимому
    и координате X, а не по позиции с конца."""
    time_item = next((it for it in items if re.match(r"^\d{1,2}:\d{2}$", it["text"])), None)
    if time_item is None:
        return None  # шапка экрана или обрезок без времени — не карточка

    time_str = time_item["text"]
    price_val = 0.0
    rest = []

    for it in items:
        t = it["text"]
        if it is time_item:
            continue
        if "₽" in t:
            digits = re.findall(r"\d+", t)
            if digits:
                price_val = float(digits[0])
            continue
        if t in ("Проход", "Покупка"):
            continue
        rest.append(it)

    if not rest:
        print(f"[WARN] {current_date_str} {time_str}: в карточке нет названия — пропуск (карточка обрезана сверху).")
        return None

    title = rest[0]["text"]

    if title in SERVICE_WORDS:
        print(f"[WARN] {current_date_str} {time_str}: название карточки = служебный текст '{title}' — пропуск.")
        return None

    if len(rest) < 2 and not (title.startswith("Станция") or title.startswith("Остановка")):
        print(f"[WARN] {current_date_str} {time_str}: подозрительное название '{title}' (одиночное) — пропуск.")
        return None

    return (f"{current_date_str} {time_str}", title, price_val)


def is_valid_icon_crop(image_path: str) -> bool:
    """Проверяет, что кроп похож на значок, и печатает измеренные метрики,
    чтобы пороги можно было подкрутить по факту."""
    try:
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            print("[LOG][МЦК] Валидация: кроп не читается как изображение.")
            return False
        std = float(np.std(img))
        edges = cv2.Canny(img, 50, 150)
        edge_ratio = float(np.count_nonzero(edges)) / edges.size
        print(f"[LOG][МЦК] Валидация кропа: std={std:.1f} (порог 15.0), edges={edge_ratio:.3f} (порог 0.020)")
        if std < 15.0:
            print("[LOG][МЦК] Кандидат отбракован: низкий контраст (пустой или смазанный скриншот).")
            return False
        if edge_ratio < 0.02:
            print("[LOG][МЦК] Кандидат отбракован: мало контуров.")
            return False
        return True
    except Exception as e:
        print(f"[LOG][МЦК] Ошибка валидации кропа: {e}")
        return False


def create_template_once(cropped_icon, template_img: str, full_screen_img: str, icon_y1: int) -> bool:
    """Создаёт эталонный шаблон МЦК ровно один раз. Существующие файлы
    НИКОГДА не перезаписываются. Рядом сохраняется полный экран для выравнивания."""
    if os.path.exists(template_img):
        print("[LOG][МЦК] Шаблон уже существует — создание пропущено (перезапись запрещена).")
        return False

    candidate = "mcc_template.candidate.png"
    screen_copy = "mcc_template_fullscreen.png"
    try:
        cropped_icon.save(candidate, format="PNG")

        if not is_valid_icon_crop(candidate):
            os.remove(candidate)
            return False

        try:
            os.rename(candidate, template_img)  # не перезапишет существующий файл
        except FileExistsError:
            print("[LOG][МЦК] Шаблон появился из другого источника — перезапись не выполнялась.")
            os.remove(candidate)
            return False

        print("[LOG][МЦК] Создан и проверен эталонный шаблон МЦК со станции Локомотив.")

        if not os.path.exists(screen_copy) and os.path.exists(full_screen_img):
            try:
                shutil.copy2(full_screen_img, screen_copy)
                print(f"[LOG][МЦК] Сохранён полный экран для выравнивания: {screen_copy} "
                      f"(кроп брался из области x: 56..161, y: {icon_y1}..{icon_y1 + 105}).")
            except Exception as e:
                print(f"[LOG][МЦК] Копию экрана сохранить не удалось: {e}")

        return True
    except Exception as e:
        print(f"[LOG][МЦК] Не удалось создать шаблон: {e}")
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except Exception:
                pass
        return False


def fix_mcc_by_icon(title: str, title_y1: int, template_img: str,
                    full_screen_img: str, crop_icon_img: str) -> str:
    """Сверка иконки с эталоном МЦК. Все ошибки теперь видны в логе."""
    if "Станция метро" not in title:
        return title
    try:
        icon_x1 = 56
        icon_y1 = title_y1 - 25
        icon_x2 = icon_x1 + 105
        icon_y2 = icon_y1 + 105

        img = Image.open(full_screen_img)

        # Если разрешение скриншота не совпадает с координатами UI — пересчитываем.
        sx = img.width / SCREEN_SIZE[0]
        sy = img.height / SCREEN_SIZE[1]
        box = (int(icon_x1 * sx), int(icon_y1 * sy), int(icon_x2 * sx), int(icon_y2 * sy))

        cropped_icon = img.crop(box)
        cropped_icon.save(crop_icon_img)

        if os.path.exists(template_img):
            match_rate = compare_images(crop_icon_img, template_img)
            print(f"[LOG][МЦК] '{title}': совпадение с эталоном = {match_rate:.3f} (порог 0.85)")
            if match_rate > 0.85:
                return title.replace("Станция метро", "Станция МЦК")
        else:
            if "Локомотив" in title:
                print(f"[LOG][МЦК] Шаблона нет, пробую создать со станции: {title} (title_y1={title_y1})")
                if create_template_once(cropped_icon, template_img, full_screen_img, icon_y1):
                    return title.replace("Станция метро", "Станция МЦК")
    except Exception as e:
        print(f"[LOG][МЦК] Ошибка при обработке иконки '{title}': {e}")
    return title


def main():
    global SCREEN_SIZE

    driver = get_driver()
    driver.implicitly_wait(1)

    window_size = driver.get_window_size()
    width = window_size['width']
    height = window_size['height']
    SCREEN_SIZE = (width, height)

    start_x = int(width / 2)
    start_y = int(height * 0.75)
    end_y = int(height * 0.25)

    today = datetime.now()
    target_stop_date = today - timedelta(days=365)
    current_year = today.year

    max_scrolls = 250
    scroll_count = 0

    last_seen_date: datetime | None = None
    current_date_str: str = today.strftime("%Y-%m-%d")
    template_img = "mcc_template.png"
    full_screen_img = "full_screen_temp.png"
    crop_icon_img = "current_icon_temp.png"

    last_processed_signature = None

    print(f"[LOG] Окно: {width}x{height}. Лимит: {max_scrolls} скроллов. "
          f"Остановка на дате: {target_stop_date.strftime('%Y-%m-%d')}")

    try:
        while scroll_count < max_scrolls:
            print(f"\n--- Итерация/Скролл {scroll_count} ---")

            time.sleep(0.5)

            raw_items = collect_screen_items(driver)

            driver.save_screenshot(full_screen_img)

            # Разовая проверка совпадения разрешения скриншота и координат UI
            if scroll_count == 0:
                try:
                    probe = Image.open(full_screen_img)
                    print(f"[LOG] Скриншот: {probe.size[0]}x{probe.size[1]}, окно: {width}x{height}")
                    if probe.size[0] != width:
                        print("[LOG] ВНИМАНИЕ: разрешение скриншота не совпадает с координатами UI — "
                              "кропы иконок пересчитываются с масштабом.")
                except Exception as e:
                    print(f"[LOG] Не удалось прочитать скриншот для проверки: {e}")

            page_operations = []
            current_screen_signature = []

            for group in group_into_cards(raw_items):
                if group["date_text"] is not None:
                    parsed_dt = parse_date_header(group["date_text"], current_year)
                    if parsed_dt:
                        if last_seen_date and parsed_dt.month == 12 and last_seen_date.month == 1:
                            current_year -= 1
                            parsed_dt = parsed_dt.replace(year=current_year)
                        last_seen_date = parsed_dt
                        current_date_str = parsed_dt.strftime("%Y-%m-%d")
                        print(f"[LOG] Встречена плашка даты -> {current_date_str}")
                    continue

                result = parse_card_group(group["items"], current_date_str)
                if result is None:
                    continue

                full_datetime_str, title, price_val = result

                title = fix_mcc_by_icon(title, group["items"][0]["y1"],
                                        template_img, full_screen_img, crop_icon_img)

                current_screen_signature.append(f"{result[0].split(' ')[1]}_{title}")
                page_operations.append((full_datetime_str, title, price_val))

            if current_screen_signature == last_processed_signature and scroll_count > 0:
                print("[LOG] Контент экрана не изменился после скролла. Достигнут самый низ истории. Выход.")
                break
            last_processed_signature = current_screen_signature

            if page_operations:
                for dt, tr, pr in page_operations:
                    print(f"  [PARSED] {dt} -> {tr} (Стоимость: {pr} ₽)")
                save_to_json(page_operations)
            else:
                print("[LOG] На этой итерации не извлечено новых операций проходов.")

            if last_seen_date and last_seen_date < target_stop_date:
                print(f"[LOG] Достигли целевой даты {last_seen_date.strftime('%Y-%m-%d')}. Завершение.")
                break

            drag_up_and_stop(driver, start_x, start_y, end_y)
            scroll_count += 1

    except Exception as main_error:
        print(f"\n[КРИТИЧЕСКАЯ ОШИБКА]: {main_error}")
    finally:
        for temp_file in [full_screen_img, crop_icon_img]:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
        driver.quit()


if __name__ == "__main__":
    main()
