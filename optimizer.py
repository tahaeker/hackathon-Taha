import pandas as pd
import numpy as np
from datetime import timedelta
from typing import List, Dict, Any

from data_loader import haversine
from predictor import DelayPredictor

# Base travel speeds (km/h) per road type under ideal conditions
BASE_SPEED = {
    "highway": 90.0,
    "urban": 45.0,
    "rural": 65.0,
    "mountain": 40.0,
}
DEFAULT_SPEED = 55.0


def _travel_minutes(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    road_type: str,
    circuity: dict,
    delay_factor: float,
) -> float:
    straight_km = haversine(lat1, lon1, lat2, lon2)
    cf = circuity.get(road_type, 1.4)
    road_km = straight_km * cf
    speed = BASE_SPEED.get(road_type, DEFAULT_SPEED)
    ideal_min = (road_km / speed) * 60.0
    return ideal_min * delay_factor


def _stop_result(
    stop: pd.Series,
    arrival: pd.Timestamp,
    travel_min: float,
    stop_delay_min: float,
    new_sequence: int,
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
    }


def simulate_route(
    stops_df: pd.DataFrame,
    route_info: dict,
    predictor: DelayPredictor,
    hist_df: pd.DataFrame,
    weather_condition: str,
    traffic_level: str,
    circuity: dict,
) -> List[dict]:
    """Simulate the original stop order and predict arrival times."""
    results = []
    delay_factor = float(route_info.get("predicted_delay_factor", 1.0))
    current_time = pd.Timestamp(route_info["departure_planned"])

    prev_lat = float(stops_df.iloc[0]["latitude"])
    prev_lon = float(stops_df.iloc[0]["longitude"])

    for idx, (_, stop) in enumerate(stops_df.iterrows()):
        lat, lon = float(stop["latitude"]), float(stop["longitude"])
        road_type = str(stop["road_type"])

        if idx == 0:
            travel_min = float(stop["planned_travel_min"]) * delay_factor
        else:
            travel_min = _travel_minutes(
                prev_lat, prev_lon, lat, lon, road_type, circuity, delay_factor
            )

        current_time += timedelta(minutes=travel_min)

        stop_delay = predictor.predict_stop_delay(
            road_type=road_type,
            traffic_level=traffic_level,
            weather_condition=weather_condition,
            hour=current_time.hour,
            hist_df=hist_df,
        )

        results.append(_stop_result(stop, current_time, travel_min, stop_delay, idx + 1))
        current_time += timedelta(minutes=float(stop["planned_service_min"]))
        prev_lat, prev_lon = lat, lon

    return results


def optimize_stop_order(
    stops_df: pd.DataFrame,
    route_info: dict,
    predictor: DelayPredictor,
    hist_df: pd.DataFrame,
    weather_condition: str,
    traffic_level: str,
    circuity: dict,
) -> List[dict]:
    """
    Greedy nearest-neighbor optimizer with time-window urgency scoring.
    Prioritizes stops whose time window closes soonest when two candidates
    are equidistant, heavily penalizes stops that are already unreachable.
    """
    delay_factor = float(route_info.get("predicted_delay_factor", 1.0))
    current_time = pd.Timestamp(route_info["departure_planned"])

    # Start from depot — use first stop's coords as proxy
    cur_lat = float(stops_df.iloc[0]["latitude"])
    cur_lon = float(stops_df.iloc[0]["longitude"])

    remaining = stops_df.copy().reset_index(drop=True)
    visited: List[dict] = []

    while not remaining.empty:
        best_score = float("inf")
        best_idx = 0

        for i, row in remaining.iterrows():
            lat, lon = float(row["latitude"]), float(row["longitude"])
            road_type = str(row["road_type"])
            travel = _travel_minutes(cur_lat, cur_lon, lat, lon, road_type, circuity, delay_factor)
            eta = current_time + timedelta(minutes=travel)

            window_close = pd.Timestamp(row["time_window_close"])
            window_open = pd.Timestamp(row["time_window_open"])

            # Penalty: can we arrive before window closes?
            minutes_until_close = (window_close - current_time).total_seconds() / 60.0
            if minutes_until_close < travel:
                # Already missed — penalise proportionally to how late
                lateness = travel - minutes_until_close
                penalty = 1000.0 + lateness * 2.0
            else:
                penalty = 0.0

            # Urgency bonus: prioritise stops closing soon
            urgency_bonus = max(0.0, 500.0 - minutes_until_close) * 0.1

            score = travel + penalty - urgency_bonus
            if score < best_score:
                best_score = score
                best_idx = i

        chosen = remaining.loc[best_idx]
        lat, lon = float(chosen["latitude"]), float(chosen["longitude"])
        road_type = str(chosen["road_type"])
        travel_min = _travel_minutes(cur_lat, cur_lon, lat, lon, road_type, circuity, delay_factor)

        current_time += timedelta(minutes=travel_min)

        stop_delay = predictor.predict_stop_delay(
            road_type=road_type,
            traffic_level=traffic_level,
            weather_condition=weather_condition,
            hour=current_time.hour,
            hist_df=hist_df,
        )

        visited.append(
            _stop_result(chosen, current_time, travel_min, stop_delay, len(visited) + 1)
        )
        current_time += timedelta(minutes=float(chosen["planned_service_min"]))
        cur_lat, cur_lon = lat, lon
        remaining = remaining.drop(best_idx).reset_index(drop=True)

    return visited


def compute_metrics(stops_results: List[dict]) -> dict:
    total = len(stops_results)
    if total == 0:
        return {"total_stops": 0, "on_time_stops": 0, "on_time_rate": 0.0,
                "total_predicted_delay_min": 0.0, "avg_delay_per_stop_min": 0.0}

    on_time = sum(1 for s in stops_results if s["within_time_window"])
    total_delay = sum(s["predicted_stop_delay_min"] for s in stops_results)

    return {
        "total_stops": total,
        "on_time_stops": on_time,
        "on_time_rate": round(on_time / total, 3),
        "total_predicted_delay_min": round(total_delay, 1),
        "avg_delay_per_stop_min": round(total_delay / total, 1),
    }
