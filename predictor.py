"""
predictor.py
============
Random Forest Regressor ile rota gecikme tahmini.

Eğitim verisi: data_loader.build_training_features() tarafından üretilen
birleşik feature matrisi (hava + trafik + durak istatistikleri).

Stop seviyesi gecikme: historical_delay_stats tablosuna lookup.
"""

import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score

from data_loader import (
    load_all,
    build_training_features,
    MODEL_FEATURE_COLS,
)

WEATHER_ORDER = ["clear", "cloudy", "fog", "wind", "rain", "snow"]
TRAFFIC_ORDER = ["low", "moderate", "high", "congested"]
VEHICLE_ORDER = ["motorcycle", "car", "van", "truck"]


def _ordinal(val: str, order: list) -> int:
    try:
        return order.index(str(val))
    except ValueError:
        return len(order) // 2


def hour_to_bucket(hour: int) -> str:
    if 5 <= hour <= 6:
        return "early_morning"
    if 7 <= hour <= 9:
        return "morning_rush"
    if 10 <= hour <= 16:
        return "midday"
    if 17 <= hour <= 19:
        return "evening_rush"
    return "night"


class DelayPredictor:
    def __init__(self):
        self.model = RandomForestRegressor(
            n_estimators=200,
            max_depth=14,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1,
        )
        self.is_trained = False
        self._feature_cols: list = []

    def train(
        self,
        routes: pd.DataFrame,
        stops: pd.DataFrame,
        traffic: pd.DataFrame,
        weather: pd.DataFrame,
    ) -> dict:
        """
        build_training_features() ile birleşik matris üretir,
        Random Forest eğitir, test metrikleri döndürür.
        """
        full_df = build_training_features(routes, stops, traffic, weather)

        # Gerçekten var olan sütunları kullan
        available = [c for c in MODEL_FEATURE_COLS if c in full_df.columns]
        self._feature_cols = available

        X = full_df[available].fillna(0)
        y = full_df["target_delay_min"]

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        self.model.fit(X_train, y_train)
        preds = self.model.predict(X_test)
        self.is_trained = True

        importances = dict(zip(available, self.model.feature_importances_.round(4)))
        return {
            "mae_min": round(mean_absolute_error(y_test, preds), 2),
            "r2": round(r2_score(y_test, preds), 3),
            "train_size": len(X_train),
            "test_size": len(X_test),
            "top_features": dict(
                sorted(importances.items(), key=lambda x: -x[1])[:6]
            ),
            "features_used": len(available),
        }

    def predict_route_delay(
        self,
        weather_condition: str,
        temperature_c: float,
        precipitation_mm: float,
        wind_speed_kmh: float,
        humidity_pct: float,
        visibility_km: float,
        traffic_level: str,
        road_incident: int,
        incident_severity: float,
        num_stops: int,
        total_distance_km: float,
        hour: int,
        day_of_week: int,
        vehicle_type: str,
        # opsiyonel zenginleştirilmiş özellikler
        nearest_congestion_ratio: float = 0.2,
        avg_delay_probability: float = 0.3,
        max_delay_probability: float = 0.5,
        pct_mountain: float = 0.0,
        pct_highway: float = 0.5,
        pct_urban: float = 0.3,
        pct_rural: float = 0.2,
        stop_travel_ratio: float = 1.2,
        total_packages: int = 30,
        total_weight_kg: float = 100.0,
        nearest_weather_risk: float = 0.3,
        month: int = 6,
    ) -> float:
        weather_risk = (
            min(precipitation_mm, 20) / 20.0 * 0.4
            + (1.0 - min(visibility_km, 30) / 30.0) * 0.3
            + min(wind_speed_kmh, 60) / 60.0 * 0.3
        )
        row_data = {
            "weather_enc": _ordinal(weather_condition, WEATHER_ORDER),
            "temperature_c": temperature_c,
            "precipitation_mm": precipitation_mm,
            "wind_speed_kmh": wind_speed_kmh,
            "humidity_pct": humidity_pct,
            "visibility_km": visibility_km,
            "weather_risk": weather_risk,
            "traffic_enc": _ordinal(traffic_level, TRAFFIC_ORDER),
            "nearest_congestion_ratio": nearest_congestion_ratio,
            "road_incident": road_incident,
            "incident_severity": incident_severity,
            "vehicle_enc": _ordinal(vehicle_type, VEHICLE_ORDER),
            "num_stops": num_stops,
            "total_distance_km": total_distance_km,
            "hour": hour,
            "day_of_week": day_of_week,
            "month": month,
            "is_rush_hour": 1 if hour in range(7, 10) or hour in range(17, 20) else 0,
            "is_night": 1 if hour >= 22 or hour <= 5 else 0,
            "avg_delay_probability": avg_delay_probability,
            "max_delay_probability": max_delay_probability,
            "pct_mountain": pct_mountain,
            "pct_highway": pct_highway,
            "pct_urban": pct_urban,
            "pct_rural": pct_rural,
            "stop_travel_ratio": stop_travel_ratio,
            "total_packages": total_packages,
            "total_weight_kg": total_weight_kg,
            "nearest_weather_risk": nearest_weather_risk,
        }
        row = pd.DataFrame([{k: row_data[k] for k in self._feature_cols if k in row_data}])
        row = row.reindex(columns=self._feature_cols, fill_value=0)
        return max(0.0, float(self.model.predict(row)[0]))

    def predict_stop_delay(
        self,
        road_type: str,
        traffic_level: str,
        weather_condition: str,
        hour: int,
        hist_df: pd.DataFrame,
    ) -> float:
        """
        historical_delay_stats tablosundan (road_type, traffic, weather, time_bucket)
        kombinasyonu için ortalama gecikme döndürür.
        Kademeli fallback: tam eşleşme → road+traffic → traffic → sabit.
        """
        bucket = hour_to_bucket(hour)

        mask = (
            (hist_df["road_type"] == road_type)
            & (hist_df["traffic_level"] == traffic_level)
            & (hist_df["weather_condition"] == weather_condition)
            & (hist_df["time_bucket"] == bucket)
        )
        match = hist_df[mask]
        if not match.empty:
            return float(match.iloc[0]["mean_delay_min"])

        mask2 = (hist_df["road_type"] == road_type) & (hist_df["traffic_level"] == traffic_level)
        match2 = hist_df[mask2]
        if not match2.empty:
            return float(match2["mean_delay_min"].mean())

        mask3 = hist_df["traffic_level"] == traffic_level
        match3 = hist_df[mask3]
        if not match3.empty:
            return float(match3["mean_delay_min"].mean())

        return 10.0
