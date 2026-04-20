"""
optimizer.py
============
Dinamik rota simülasyonu ve optimizasyon.

Her durak hesaplanırken current_time ve durağın lat/lon'u kullanılarak
o ana ait hava ve trafik verisi çekilir. Bu sayede sıra değişikliğinden
veya gecikmeden doğan çevresel farklılıklar tahmine yansır.

Akış:
  1. simulate_route  — mevcut sırayı simüle eder (karşılaştırma bazı)
  2. optimize_stop_order — greedy nearest-neighbor + time-window urgency
  3. Her iki fonksiyon da per-stop dinamik lookup kullanır
"""

import pandas as pd
import numpy as np
from datetime import timedelta
from typing import List

from data_loader import haversine, get_weather_at, get_traffic_at, lookup_effective_speed
from predictor import DelayPredictor


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _travel_minutes(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    road_type: str,
    traffic_level: str,
    weather_condition: str,
    circuity: dict,
    delay_factor: float,
    speed_table: dict,
) -> float:
    """
    A→C kenarı için seyahat süresi tahmini.
    Hız, veri-güdümlü speed_table'dan (road × traffic × weather) çekilir.
    """
    straight_km = haversine(lat1, lon1, lat2, lon2)
    cf = circuity.get(road_type, 1.4)
    road_km = straight_km * cf
    speed = lookup_effective_speed(road_type, traffic_level, weather_condition, speed_table)
    return (road_km / speed) * 60.0 * delay_factor


def _dynamic_conditions(
    lat: float,
    lon: float,
    road_type: str,
    current_time: pd.Timestamp,
    weather_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
) -> dict:
    """
    Durağın konumuna ve o anki saate bakarak dinamik hava + trafik döndürür.
    Bu fonksiyon sayesinde 'saat değişince koşullar değişir' mantığı kurulur.
    """
    w = get_weather_at(lat, lon, current_time, weather_df)
    t = get_traffic_at(lat, lon, current_time, road_type, traffic_df)

    return {
        "weather_condition": str(w["weather_condition"]),
        "traffic_level": str(t["traffic_level"]),
        "congestion_ratio": float(t["congestion_ratio"]),
        "delay_risk_score": float(w["delay_risk_score"]),
        "road_surface": str(w.get("road_surface_condition", "dry")),
        "precipitation_mm": float(w["precipitation_mm"]),
        "visibility_km": float(w["visibility_km"]),
        "wind_speed_kmh": float(w["wind_speed_kmh"]),
    }


def _stop_result(
    stop: pd.Series,
    arrival: pd.Timestamp,
    travel_min: float,
    stop_delay_min: float,
    new_sequence: int,
    dynamic: dict,
) -> dict:
    window_open = pd.Timestamp(stop["time_window_open"])
    window_close = pd.Timestamp(stop["time_window_close"])
    in_window = window_open <= arrival <= window_close

    return {
        "stop_id": str(stop["stop_id"]),
        "stop_sequence": new_sequence,
        "original_sequence": int(stop["stop_sequence"]),
        "latitude": float(stop["latitude"]),
        "longitude": float(stop["longitude"]),
        "road_type": str(stop["road_type"]),
        "predicted_arrival": arrival.isoformat(),
        "time_window_open": window_open.isoformat(),
        "time_window_close": window_close.isoformat(),
        "within_time_window": bool(in_window),
        "predicted_travel_min": round(travel_min, 1),
        "predicted_stop_delay_min": round(stop_delay_min, 1),
        "package_count": int(stop["package_count"]),
        "package_weight_kg": float(stop["package_weight_kg"]),
        # Dinamik çevresel koşullar — hangi değerlerin kullanıldığını gösterir
        "dynamic_conditions": {
            "weather": dynamic["weather_condition"],
            "traffic": dynamic["traffic_level"],
            "congestion_ratio": round(dynamic["congestion_ratio"], 3),
            "delay_risk_score": round(dynamic["delay_risk_score"], 3),
            "road_surface": dynamic["road_surface"],
        },
    }


# ---------------------------------------------------------------------------
# Simülasyon (orijinal sıra)
# ---------------------------------------------------------------------------

def simulate_route(
    stops_df: pd.DataFrame,
    route_info: dict,
    predictor: DelayPredictor,
    hist_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
    circuity: dict,
    speed_table: dict,
    initial_weather: str,
    initial_traffic: str,
) -> List[dict]:
    """
    Mevcut durak sırasını simüle eder.

    Her durak için:
      1. current_time ve durağın (lat, lon) kullanılarak dinamik hava+trafik çekilir.
      2. Dinamik traffic_level ile historical_delay_stats'tan stop gecikme alınır.
      3. Dinamik congestion_ratio ile seyahat süresi düzeltilir.
    """
    results = []
    # delay_factor artık SADECE ilk stop'un planned_travel_min'ini düzeltmek için.
    # Sonraki stop'lar effective_speed (gerçek kurye hızı) üzerinden hesaplanıyor,
    # böylece çift sayma (delay_factor × congestion × stop_delay) yapılmıyor.
    delay_factor = float(route_info.get("predicted_delay_factor", 1.0))
    current_time = pd.Timestamp(route_info["departure_planned"])

    prev_lat = float(stops_df.iloc[0]["latitude"])
    prev_lon = float(stops_df.iloc[0]["longitude"])

    for idx, (_, stop) in enumerate(stops_df.iterrows()):
        lat = float(stop["latitude"])
        lon = float(stop["longitude"])
        road_type = str(stop["road_type"])

        # --- Dinamik koşulları çek ---
        dynamic = _dynamic_conditions(lat, lon, road_type, current_time, weather_df, traffic_df)

        if idx == 0:
            # İlk stop: planned_travel_min koşullara göre ayarlanmamış flat bir değer,
            # bu yüzden delay_factor ile düzeltiyoruz.
            travel_min = float(stop["planned_travel_min"]) * delay_factor
        else:
            # Sonraki stop'lar: effective_speed (road × traffic × weather) zaten
            # gerçek kurye verisinden geldiği için ekstra çarpan UYGULAMIYORUZ.
            travel_min = _travel_minutes(
                prev_lat, prev_lon, lat, lon, road_type,
                dynamic["traffic_level"], dynamic["weather_condition"],
                circuity, delay_factor=1.0, speed_table=speed_table,
            )

        current_time += timedelta(minutes=travel_min)

        # --- Dinamik hava+trafik ile stop gecikme tahmini ---
        stop_delay = predictor.predict_stop_delay(
            road_type=road_type,
            traffic_level=dynamic["traffic_level"],
            weather_condition=dynamic["weather_condition"],
            hour=current_time.hour,
            hist_df=hist_df,
        )

        results.append(_stop_result(stop, current_time, travel_min, stop_delay, idx + 1, dynamic))
        # Cascade effect: stop_delay propagates into subsequent ETAs
        current_time += timedelta(minutes=float(stop["planned_service_min"]) + stop_delay)
        prev_lat, prev_lon = lat, lon

    return results


# ---------------------------------------------------------------------------
# Optimizasyon (greedy nearest-neighbor + time-window urgency)
# ---------------------------------------------------------------------------

def optimize_stop_order(
    stops_df: pd.DataFrame,
    route_info: dict,
    predictor: DelayPredictor,
    hist_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
    circuity: dict,
    speed_table: dict,
    initial_weather: str,
    initial_traffic: str,
) -> List[dict]:
    """
    Greedy nearest-neighbor optimizasyon.

    Her adım kandidatları puanlarken o kandidatın konumuna ve ETA'sına
    göre dinamik koşullar çekilir. Böylece tıkanık bir bölgeyi gece
    geçmek ile rush-hour'da geçmek farklı skorlanır.

    Skor = seyahat_süresi + pencere_cezası - aciliyet_bonusu
    (Seyahat süresi artık effective_speed'ten geldiği için ekstra çarpan yok.)
    """
    # delay_factor burada KULLANILMIYOR çünkü tüm travel'lar speed_table'dan geliyor.
    current_time = pd.Timestamp(route_info["departure_planned"])
    cur_lat = float(stops_df.iloc[0]["latitude"])
    cur_lon = float(stops_df.iloc[0]["longitude"])

    remaining = stops_df.copy().reset_index(drop=True)
    visited: List[dict] = []

    while not remaining.empty:
        best_score = float("inf")
        best_idx = 0
        best_dynamic = {}
        best_travel = 0.0

        for i, row in remaining.iterrows():
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            road_type = str(row["road_type"])

            # Seyahat süresi doğrudan effective_speed tablosundan — ekstra çarpan yok.
            travel = _travel_minutes(
                cur_lat, cur_lon, lat, lon, road_type,
                initial_traffic, initial_weather,
                circuity, delay_factor=1.0, speed_table=speed_table,
            )

            # Dinamik koşulları ETA'da çek (stop_delay ve UI için bilgi amaçlı)
            eta = current_time + timedelta(minutes=travel)
            dyn = _dynamic_conditions(lat, lon, road_type, eta, weather_df, traffic_df)

            window_close = pd.Timestamp(row["time_window_close"])
            minutes_until_close = (window_close - current_time).total_seconds() / 60.0

            if minutes_until_close < travel:
                # Pencere kesinlikle kaçacak — dengeli ceza
                overrun = travel - minutes_until_close
                penalty = 2000.0 + overrun * 8.0
            elif minutes_until_close < travel * 1.20:
                # Koruma bölgesi — sıkışık pencere
                margin_pct = (minutes_until_close - travel) / (travel * 0.20)
                penalty = 300.0 * (1.0 - margin_pct)
            else:
                penalty = 0.0

            # Aciliyet bonusu — 120 dakikaya kadar üstel
            urgency_bonus = 900.0 / max(1.0, minutes_until_close) if minutes_until_close < 120 else 0.0

            score = travel + penalty - urgency_bonus
            if score < best_score:
                best_score = score
                best_idx = i
                best_dynamic = dyn
                best_travel = travel

        chosen = remaining.loc[best_idx]
        lat = float(chosen["latitude"])
        lon = float(chosen["longitude"])
        road_type = str(chosen["road_type"])

        current_time += timedelta(minutes=best_travel)

        # Varışta bir kez daha dinamik koşul çek (gerçek varış saatinde)
        final_dynamic = _dynamic_conditions(lat, lon, road_type, current_time, weather_df, traffic_df)

        stop_delay = predictor.predict_stop_delay(
            road_type=road_type,
            traffic_level=final_dynamic["traffic_level"],
            weather_condition=final_dynamic["weather_condition"],
            hour=current_time.hour,
            hist_df=hist_df,
        )

        visited.append(
            _stop_result(chosen, current_time, best_travel, stop_delay, len(visited) + 1, final_dynamic)
        )
        # Cascade effect: stop_delay propagates into subsequent ETAs
        current_time += timedelta(minutes=float(chosen["planned_service_min"]) + stop_delay)
        cur_lat, cur_lon = lat, lon
        remaining = remaining.drop(best_idx).reset_index(drop=True)

    return visited


# ---------------------------------------------------------------------------
# Metrik hesaplama
# ---------------------------------------------------------------------------

def compute_metrics(stops_results: List[dict]) -> dict:
    total = len(stops_results)
    if total == 0:
        return {
            "total_stops": 0, "on_time_stops": 0, "on_time_rate": 0.0,
            "total_predicted_delay_min": 0.0, "avg_delay_per_stop_min": 0.0,
        }
    on_time = sum(1 for s in stops_results if s["within_time_window"])
    total_delay = sum(s["predicted_stop_delay_min"] for s in stops_results)
    return {
        "total_stops": total,
        "on_time_stops": on_time,
        "on_time_rate": round(on_time / total, 3),
        "total_predicted_delay_min": round(total_delay, 1),
        "avg_delay_per_stop_min": round(total_delay / total, 1),
    }
