import json
import os
import pandas as pd

# ================== ПАРАМЕТРЫ СРАВНЕНИЯ ==================
WALLET_WINDOW_PRICE = 75.0      # старт окна 90 минут по кошельку (любой транспорт)
TAT_ADDON = 37.0                # доплата до тарифа 90 минут (разница 112 - 75)
SUBSCRIPTION_PRICE = 24900.0    # Единый 365 за год
TOLERANCE = 3000.0              # бюджет неудобства: приемлемая надбавка за абонемент
MAX_PERIOD_DAYS = 365           # анализ не дальше чем за год от свежей записи

SERVICE_WORDS = {
    "История", "Проходы", "Покупки", "Операции", "Операция", "Фильтры",
    "Проход", "Покупка", "Мед. карта", "Маршрут", "Кабинет", "Город", "Новости",
}


def get_rail_subtype(station_name: str) -> str | None:
    name_clean = station_name.strip().lower()
    if "мцд" in name_clean:
        return "MCD"
    if "мцк" in name_clean or any(
        x in name_clean
        for x in ["локомотив", "соколиная гора", "измайлово", "ростокино", "новохохловская"]
    ):
        return "MCC"
    if "метро" in name_clean:
        return "METRO"
    return None


def is_surface_transport(station_name: str) -> bool:
    name_lower = station_name.lower()
    return any(x in name_lower for x in ["автобус", "трамвай", "электробус", "маршрут", "т1"])


def load_clean_data(file_path: str) -> pd.DataFrame:
    with open(file_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    clean_rows = []
    skipped = 0
    for item in raw_data:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            skipped += 1
            continue
        timestamp_str, transport, recorded_price = item[0], item[1], item[2]
        if timestamp_str is None or "None" in str(timestamp_str):
            skipped += 1
            continue
        transport_str = str(transport).strip()
        if transport_str in SERVICE_WORDS:
            skipped += 1
            continue
        try:
            recorded_price = float(recorded_price)
        except (TypeError, ValueError):
            recorded_price = 0.0
        clean_rows.append([str(timestamp_str), transport_str, recorded_price])

    df = pd.DataFrame(clean_rows, columns=["timestamp", "transport", "recorded_price"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).drop_duplicates(subset=["timestamp", "transport"])
    df = df.sort_values(by="timestamp").reset_index(drop=True)
    print(f"[LOG] Валидных записей: {len(df)}, отброшено мусорных: {skipped}")
    return df


def simulate_wallet(df: pd.DataFrame) -> pd.DataFrame:
    """Прогоняет тарифную модель кошелька по ВСЕМ данным (состояние окна
    должно быть честным и на границах периода тоже)."""
    simulated_costs = []
    billing_notes = []

    window_start_time = None
    visited_rail_subtypes = set()
    last_rail_type = None
    has_tat_addon_charge = False

    for _, row in df.iterrows():
        current_time = row["timestamp"]
        transport = row["transport"]
        recorded_price = float(row["recorded_price"])
        rail_subtype = get_rail_subtype(transport)

        # Платные поездки (дальнее МЦД, пригород) нейтральны: одинаковы
        # в обоих сценариях, окно не сбрасывают.
        if recorded_price > 0:
            simulated_costs.append(0.0)
            billing_notes.append(f"Платная поездка — в сравнении не участвует: {transport}")
            continue

        if window_start_time is None or (current_time - window_start_time).total_seconds() > 5400:
            window_start_time = current_time
            visited_rail_subtypes = set()
            has_tat_addon_charge = False
            if rail_subtype:
                last_rail_type = rail_subtype
                visited_rail_subtypes.add(rail_subtype)
            else:
                last_rail_type = None
            simulated_costs.append(WALLET_WINDOW_PRICE)
            billing_notes.append("Новое окно 90 минут: старт (75 ₽)")
            continue

        if is_surface_transport(transport):
            if last_rail_type is not None and not has_tat_addon_charge:
                cost = TAT_ADDON
                has_tat_addon_charge = True
                billing_notes.append("Доплата до тарифа 90 минут: наземный после рельсов (+37 ₽)")
            else:
                cost = 0.0
                billing_notes.append("Бесплатный проезд на наземном транспорте")
        elif rail_subtype:
            if last_rail_type is None:
                last_rail_type = rail_subtype
                visited_rail_subtypes.add(rail_subtype)
                if not has_tat_addon_charge:
                    cost = TAT_ADDON
                    has_tat_addon_charge = True
                    billing_notes.append("Пересадка наземный ➔ рельсы: доплата до тарифа (+37 ₽)")
                else:
                    cost = 0.0
                    billing_notes.append("Бесплатный переход на рельсы")
            else:
                if rail_subtype not in visited_rail_subtypes:
                    cost = 0.0
                    visited_rail_subtypes.add(rail_subtype)
                    last_rail_type = rail_subtype
                    billing_notes.append(f"Бесплатная рельсовая пересадка: {rail_subtype}")
                else:
                    window_start_time = current_time
                    visited_rail_subtypes = {rail_subtype}
                    has_tat_addon_charge = False
                    last_rail_type = rail_subtype
                    cost = WALLET_WINDOW_PRICE
                    billing_notes.append(f"Повторный вход на подтип {rail_subtype} (новое окно: 75 ₽)")
        else:
            cost = WALLET_WINDOW_PRICE
            billing_notes.append("Обычный проход")

        simulated_costs.append(cost)

    df["simulated_cost"] = simulated_costs
    df["billing_note"] = billing_notes
    return df


def find_analysis_period(df: pd.DataFrame):
    """Границы анализа по РЕАЛЬНЫМ данным:
    конец = самая свежая запись; начало = максимум из
      а) конец минус 365 дней;
      б) первый день месяца, следующего за самым свежим календарным месяцем
         без единой поездки (месяц без поездок = сбор данных туда не дошёл,
         всё старше ненадёжно)."""
    end = df["timestamp"].max()
    earliest_allowed = (end - pd.Timedelta(days=MAX_PERIOD_DAYS)).normalize()

    window_df = df[df["timestamp"] >= earliest_allowed]
    if window_df.empty:
        return end, end

    months_with_trips = set(window_df["timestamp"].dt.to_period("M"))
    start = window_df["timestamp"].min()

    empty_month = None
    cur = earliest_allowed.to_period("M")
    last_month = end.to_period("M")
    while cur <= last_month:
        if cur not in months_with_trips:
            empty_month = cur  # идём по возрастанию — останется самый свежий
        cur = cur + 1

    if empty_month is not None:
        gap_start = (empty_month + 1).to_timestamp()
        if gap_start > start:
            print(f"[LOG] Найден месяц без поездок: {empty_month}. "
                  f"Сбор данных туда не дошёл — данные старше {gap_start.date()} игнорируются.")
            start = gap_start
    return start, end


def calculate_wallet_simulation(file_path: str = "history.json"):
    if not os.path.exists(file_path):
        print(f"[Ошибка] Файл {file_path} не найден.")
        return

    df = load_clean_data(file_path)
    if df.empty:
        print("[Ошибка] После очистки не осталось валидных записей.")
        return

    df = simulate_wallet(df)

    start, end = find_analysis_period(df)
    dfp = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].reset_index(drop=True)
    dropped = len(df) - len(dfp)
    print(f"[LOG] Период анализа: {start.date()} .. {end.date()} "
          f"({len(dfp)} записей, старых/вне периода отброшено: {dropped})")

    dfp["month"] = dfp["timestamp"].dt.to_period("M").astype(str)
    monthly = dfp.groupby("month").agg(
        trips=("transport", "size"),
        days=("timestamp", lambda s: s.dt.date.nunique()),
        cost=("simulated_cost", "sum"),
    )

    total = float(dfp["simulated_cost"].sum())
    covered_days = (end.normalize() - start.normalize()).days + 1

    print("\n================ РЕЗУЛЬТАТЫ СИМУЛЯЦИИ (КОШЕЛЁК / 90 МИНУТ) ================")
    print(monthly.to_string())
    print(f"\nИтого по кошельку за реальный период {start.date()} .. {end.date()} ({covered_days} дн.): {total:.0f} ₽")

    # Сопоставимые величины
    sub_prorated = SUBSCRIPTION_PRICE * covered_days / 365.0
    diff_prorated = sub_prorated - total
    print(f"Единый за тот же период (пропорция года): {sub_prorated:.0f} ₽")

    if covered_days >= MAX_PERIOD_DAYS - 1:
        print("Период покрывает полный год — экстраполяция не требуется.")
        annual_projection = total
    else:
        annual_projection = total * 365.0 / covered_days
        months_covered = round(covered_days / 30.44, 1)
        print(f"Данных меньше года ({months_covered} мес.) — прогноз на 365 дней: "
              f"{annual_projection:.0f} ₽ (предполагает, что непокрытый период похож на наблюдаемый)")
    if covered_days < 120:
        print("[ВНИМАНИЕ] Данных меньше 4 месяцев — прогноз на год очень грубый, решение принимайте осторожно.")

    diff_annual = SUBSCRIPTION_PRICE - annual_projection
    print(f"\nЕдиный 365: {SUBSCRIPTION_PRICE:.0f} ₽ (бюджет неудобства ±{TOLERANCE:.0f} ₽)")
    if diff_annual > TOLERANCE:
        verdict = f"Абонемент ВЫГОДНЕЕ на {diff_annual:.0f} ₽ в год"
    elif diff_annual < -TOLERANCE:
        verdict = f"Кошелёк ВЫГОДНЕЕ на {-diff_annual:.0f} ₽ в год"
    else:
        verdict = f"Разница {diff_annual:.0f} ₽ в год — в пределах бюджета неудобства, решение по удобству"
    print(f"ВЕРДИКТ: {verdict}")

    dfp.to_csv("final_clean_report.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    calculate_wallet_simulation()
