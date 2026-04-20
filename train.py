"""
train.py
========
Modeli sıfırdan eğitir ve models/delay_predictor.pkl dosyasına kaydeder.

Kullanım:
    python train.py

Ne zaman çalıştırılır:
    - data/*.csv dosyaları güncellendiğinde
    - Yeni feature eklendiğinde (data_loader.MODEL_FEATURE_COLS değişince)
    - İlk deploy öncesi (VM'e .pkl'i ship et veya orada çalıştır)
"""

import json
from pathlib import Path

from data_loader import load_all
from predictor import DelayPredictor

MODEL_PATH = Path(__file__).parent / "models" / "delay_predictor.pkl"
STATS_PATH = Path(__file__).parent / "models" / "train_stats.json"


def main() -> None:
    print("Veri yükleniyor...")
    routes, stops, traffic, weather, _hist = load_all()
    print(f"  routes: {len(routes)} | stops: {len(stops)} | traffic: {len(traffic)} | weather: {len(weather)}")

    print("Random Forest eğitiliyor...")
    predictor = DelayPredictor()
    stats = predictor.train(routes, stops, traffic, weather)

    predictor.save(MODEL_PATH)
    STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 50)
    print(f"Model kaydedildi : {MODEL_PATH}")
    print(f"Metrikler        : {STATS_PATH}")
    print(f"  MAE  : {stats['mae_min']} dakika")
    print(f"  R²   : {stats['r2']}")
    print(f"  Train: {stats['train_size']} | Test: {stats['test_size']}")
    print(f"  Top features: {list(stats['top_features'].keys())}")
    print("=" * 50)


if __name__ == "__main__":
    main()
