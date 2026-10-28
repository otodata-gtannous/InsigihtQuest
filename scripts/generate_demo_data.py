"""Regenerate the fictional demo_data.csv deterministically.

Usage (from the repo root):  .venv\\Scripts\\python.exe scripts\\generate_demo_data.py

Everything here is synthetic. PurchaseDate mimics the warranty-start proxy used
by the real flat table: installs lag it by 5-120 days, some devices are never
installed (inventory on hand), and a few rows carry invalid lags on purpose.
"""

from __future__ import annotations

import csv
import math
import random
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

SEED = 20261001
AS_OF = date(2026, 10, 1)
FIRST_MONTH = date(2023, 1, 1)
N_MONTHS = 45  # 2023-01 .. 2026-09
SCALE = 0.8  # tunes row count (and file size) without changing the shapes
OUTPUT = Path(__file__).resolve().parents[1] / "demo_data.csv"

COLUMNS = [
    "Id", "CompanyId", "DeviceType", "PurchaseDate", "PurchaseAmount",
    "DeviceInitialInstallDate", "TankInstallDate", "Latitude", "Longitude",
    "DeviceLocation", "Territory", "CustomerSize", "ShipmentDate", "EligibleUnits",
    "MarketDate", "MarketIndex", "InventoryItem", "InventoryAsOf", "InventoryOnHand",
    "InventoryDailyDemand", "ReplenishmentDays", "ReorderPoint", "MaxStock",
]

# Share of devices never installed: low => stockout risk, high => overstock.
DEVICE_TYPES = {
    "Monitor Basic": {"price": 480.0, "never_installed": 0.004, "replenishment_days": 45},
    "Monitor Plus": {"price": 520.0, "never_installed": 0.08, "replenishment_days": 14},
    "Monitor Pro": {"price": 560.0, "never_installed": 0.34, "replenishment_days": 21},
    "Tank Sensor": {"price": 600.0, "never_installed": 0.14, "replenishment_days": 14},
    "Gateway": {"price": 750.0, "never_installed": 0.30, "replenishment_days": 28},
}

TERRITORIES = {
    "Quebec": ("QC", [("Montreal", 45.50, -73.57, 5), ("Quebec City", 46.81, -71.21, 3),
                      ("Gatineau", 45.48, -75.70, 1), ("Sherbrooke", 45.40, -71.89, 1)]),
    "Ontario": ("ON", [("Toronto", 43.65, -79.38, 5), ("Ottawa", 45.42, -75.70, 2),
                       ("London", 42.98, -81.25, 2), ("Sudbury", 46.49, -80.99, 1)]),
    "Alberta": ("AB", [("Calgary", 51.05, -114.07, 3), ("Edmonton", 53.55, -113.49, 3),
                       ("Red Deer", 52.27, -113.81, 1)]),
    "British Columbia": ("BC", [("Vancouver", 49.28, -123.12, 4), ("Victoria", 48.43, -123.37, 2),
                                ("Kelowna", 49.89, -119.50, 2)]),
    "Manitoba": ("MB", [("Winnipeg", 49.90, -97.14, 4), ("Brandon", 49.85, -99.95, 1)]),
    "Saskatchewan": ("SK", [("Regina", 50.45, -104.62, 2), ("Saskatoon", 52.13, -106.67, 2)]),
    "Nova Scotia": ("NS", [("Halifax", 44.65, -63.58, 3), ("Sydney", 46.14, -60.19, 1)]),
    "New Brunswick": ("NB", [("Moncton", 46.09, -64.78, 2), ("Fredericton", 45.96, -66.64, 2),
                             ("Saint John", 45.27, -66.06, 1)]),
}

# (territory, device type, behaviour, monthly base, first month index, tiny-market row count)
MARKETS = [
    ("Quebec", "Monitor Basic", "seasonal", 28, 0, None),
    ("Quebec", "Monitor Plus", "seasonal", 16, 0, None),
    ("Quebec", "Monitor Pro", "growing", 6, 0, None),
    ("Quebec", "Tank Sensor", "stable", 10, 0, None),
    ("Quebec", "Gateway", "new", 7, 38, None),
    ("Ontario", "Monitor Basic", "noisy", 10, 0, None),
    ("Ontario", "Monitor Plus", "seasonal", 14, 0, None),
    ("Ontario", "Monitor Pro", "declining", 6, 0, None),
    ("Ontario", "Tank Sensor", "seasonal", 9, 0, None),
    ("Ontario", "Gateway", "new", 5, 35, None),
    ("Alberta", "Monitor Basic", "seasonal", 12, 0, None),
    ("Alberta", "Monitor Plus", "noisy", 8, 0, None),
    ("Alberta", "Tank Sensor", "growing", 8, 0, None),
    ("British Columbia", "Monitor Basic", "seasonal", 10, 0, None),
    ("British Columbia", "Monitor Pro", "stable", 4, 0, None),
    ("British Columbia", "Tank Sensor", "noisy", 6, 0, None),
    ("Manitoba", "Monitor Basic", "seasonal", 5, 0, None),
    ("Manitoba", "Tank Sensor", "noisy", 3, 0, None),
    ("Saskatchewan", "Monitor Basic", "stable", 4, 0, None),
    ("Saskatchewan", "Monitor Plus", "tiny", 0, 4, 13),
    ("Nova Scotia", "Monitor Basic", "stable", 3, 0, None),
    ("Nova Scotia", "Tank Sensor", "noisy", 2, 0, None),
    ("Nova Scotia", "Monitor Plus", "new", 2.5, 37, None),
    ("New Brunswick", "Monitor Basic", "tiny", 0, 5, 14),
    ("New Brunswick", "Monitor Pro", "new", 3, 38, None),
]

# Device counts for the 100+ device companies per territory.
LARGE_COMPANIES = {"Quebec": [180, 120], "Ontario": [150]}


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, count, product = math.exp(-lam), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def monthly_rate(rng: random.Random, behaviour: str, base: float, t: int, month: int) -> float:
    progress = t / (N_MONTHS - 1)
    season = 1 + 0.35 * math.sin(2 * math.pi * (month - 3) / 12)
    if behaviour == "seasonal":
        return base * season * rng.lognormvariate(0, 0.08)
    if behaviour == "stable":
        return base * rng.lognormvariate(0, 0.07)
    if behaviour == "growing":
        return base * (0.5 + 1.5 * progress) * season * rng.lognormvariate(0, 0.12)
    if behaviour == "declining":
        return base * (1.8 - 1.5 * progress) * season * rng.lognormvariate(0, 0.12)
    if behaviour == "noisy":
        rate = base * rng.lognormvariate(0, 0.65)
        if rng.random() < 0.08:
            rate *= 2.8
        return 0.0 if rng.random() < 0.07 else rate
    if behaviour == "new":
        return base * rng.lognormvariate(0, 0.25)
    raise ValueError(behaviour)


def month_start(index: int) -> date:
    year, month = divmod(FIRST_MONTH.month - 1 + index, 12)
    return date(FIRST_MONTH.year + year, month + 1, 1)


def random_day(rng: random.Random, index: int) -> date:
    first = month_start(index)
    last = min(month_start(index + 1) - timedelta(days=1), AS_OF - timedelta(days=1))
    while True:
        day = first + timedelta(days=rng.randrange((last - first).days + 1))
        if day.weekday() < 5 or rng.random() < 0.3:
            return day


def generate_purchases(rng: random.Random) -> list[dict]:
    purchases = []
    for territory, device_type, behaviour, base, start, tiny_rows in MARKETS:
        if behaviour == "tiny":
            counts = Counter(rng.sample(range(start, N_MONTHS - 1), tiny_rows))
        else:
            counts = {
                t: poisson(rng, SCALE * monthly_rate(rng, behaviour, base, t, month_start(t).month))
                for t in range(start, N_MONTHS)
            }
        for t, count in counts.items():
            purchases.extend(
                {"territory": territory, "type": device_type, "purchase": random_day(rng, t)}
                for _ in range(count)
            )
    return purchases


def build_companies(rng: random.Random, territory: str, device_count: int) -> list[dict]:
    code, cities = TERRITORIES[territory]
    sizes = list(LARGE_COMPANIES.get(territory, []))
    medium_budget = int((device_count - sum(sizes)) * 0.5) if device_count >= 150 else 0
    while medium_budget >= 10:
        size = min(medium_budget, rng.randint(10, 70))
        sizes.append(size)
        medium_budget -= size
    left = device_count - sum(sizes)
    while left > 0:
        size = min(left, 9, 1 + int(rng.expovariate(0.45)))
        sizes.append(size)
        left -= size
    rng.shuffle(sizes)
    weights = [city[3] for city in cities]
    companies = []
    for number, size in enumerate(sizes, start=1):
        city = rng.choices(cities, weights)[0]
        companies.append({
            "id": f"Demo {code} Co {number:03d}", "size": size,
            "lat": city[1] + rng.gauss(0, 0.07), "lon": city[2] + rng.gauss(0, 0.09),
        })
    return companies


def size_bucket(device_count: int) -> str:
    return "Small" if device_count < 10 else "Medium" if device_count < 100 else "Large"


def assign_companies(rng: random.Random, purchases: list[dict]) -> None:
    by_territory = defaultdict(list)
    for purchase in purchases:
        by_territory[purchase["territory"]].append(purchase)
    for territory in TERRITORIES:
        devices = by_territory[territory]
        rng.shuffle(devices)
        companies = build_companies(rng, territory, len(devices))
        slots = [company for company in companies for _ in range(company["size"])]
        for device, company in zip(devices, slots):
            device["company"] = company
            device["lat"] = company["lat"] + rng.gauss(0, 0.02)
            device["lon"] = company["lon"] + rng.gauss(0, 0.02)


def assign_installs(rng: random.Random, purchases: list[dict]) -> None:
    for device in purchases:
        purchase = device["purchase"]
        lag = min(120, 5 + int(rng.lognormvariate(math.log(16), 0.85)))
        missing = DEVICE_TYPES[device["type"]]["never_installed"]
        if (AS_OF - purchase).days <= 180:
            missing += 0.04
        install = purchase + timedelta(days=lag)
        device["install"] = None if rng.random() < missing or install >= AS_OF else install

    # Deliberately invalid rows: installs before purchase and implausibly late installs.
    installed = [d for d in purchases if d["install"] is not None]
    for device in rng.sample(installed, 8):
        device["install"] = device["purchase"] - timedelta(days=rng.randint(1, 45))
    older = [d for d in installed if d["purchase"] < date(2025, 1, 1) and d["install"] >= d["purchase"]]
    for device in rng.sample(older, 3):
        device["install"] = device["purchase"] + timedelta(days=rng.randint(400, 700))

    for device in purchases:
        install = device["install"]
        device["initial"] = (
            install - timedelta(days=rng.randint(1, 14)) if install and rng.random() < 0.3 else install
        )


def market_index(rng: random.Random) -> dict[date, float]:
    return {
        month_start(t): round(
            100 + 0.25 * t + 6 * math.sin(2 * math.pi * t / 12) + rng.gauss(0, 1.5), 1
        )
        for t in range(N_MONTHS)
    }


def inventory_by_type(purchases: list[dict]) -> dict[str, dict]:
    window_start = AS_OF - timedelta(days=90)
    result = {}
    for device_type, spec in DEVICE_TYPES.items():
        rows = [d for d in purchases if d["type"] == device_type]
        recent = sum(1 for d in rows if d["install"] and window_start <= d["install"] < AS_OF)
        demand = round(recent / 90, 2)
        days = spec["replenishment_days"]
        result[device_type] = {
            "on_hand": sum(1 for d in rows if d["install"] is None),
            "demand": demand,
            "days": days,
            "reorder": math.ceil(demand * (days + 7)),
            "max": math.ceil(demand * (days + 30)),
        }
    return result


def fmt_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def main() -> None:
    rng = random.Random(SEED)
    purchases = generate_purchases(rng)
    assign_companies(rng, purchases)
    assign_installs(rng, purchases)
    purchases.sort(key=lambda d: (d["purchase"], d["territory"], d["type"], d["company"]["id"]))

    company_sizes = Counter((d["territory"], d["company"]["id"]) for d in purchases)
    indices = market_index(rng)
    inventory = inventory_by_type(purchases)

    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        for number, device in enumerate(purchases, start=1):
            spec = DEVICE_TYPES[device["type"]]
            stock = inventory[device["type"]]
            company = device["company"]
            purchase = device["purchase"]
            lat, lon = round(device["lat"], 4), round(device["lon"], 4)
            drift = 1 + 0.02 * (purchase - FIRST_MONTH).days / 365
            amount = round(spec["price"] * drift * rng.uniform(0.97, 1.03) * 2) / 2
            month = purchase.replace(day=1)
            writer.writerow([
                f"DEMO-{number:05d}", company["id"], device["type"], purchase.isoformat(), amount,
                fmt_date(device["initial"]), fmt_date(device["install"]), lat, lon,
                f"{lat:.6f},{lon:.6f}", device["territory"],
                size_bucket(company_sizes[(device["territory"], company["id"])]),
                (purchase - timedelta(days=rng.randint(1, 21))).isoformat(), 1,
                month.isoformat(), indices[month], device["type"], AS_OF.isoformat(),
                stock["on_hand"], stock["demand"], stock["days"], stock["reorder"], stock["max"],
            ])
    print(f"Wrote {len(purchases)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()
