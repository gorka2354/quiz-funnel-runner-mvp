import json
import os
import sys
from pathlib import Path

def run_smoke_test():
    summary_path = Path("results/summary.json")
    
    # 1. Проверка существования и валидности JSON
    if not summary_path.exists():
        print(f"ERROR: {summary_path} not found. Run main.py first.")
        sys.exit(1)

    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to parse summary.json: {e}")
        sys.exit(1)

    if not isinstance(data, list):
        print("ERROR: summary.json must be a list of results.")
        sys.exit(1)

    if not data:
        print("ERROR: summary.json is empty. No funnels were processed.")
        sys.exit(1)

    # 2. Валидация каждой воронки
    for entry in data:
        slug = entry.get("slug", "unknown")
        
        # Проверка обязательных полей
        for field in ["slug", "url", "steps_total", "paywall_reached", "path"]:
            if field not in entry:
                print(f"ERROR: Missing field '{field}' in summary for {slug}")
                sys.exit(1)

        # Проверка логических условий
        if entry["steps_total"] <= 0:
            print(f"ERROR: Funnel {slug} has 0 steps.")
            sys.exit(1)

        if entry["paywall_reached"] is not True:
            print(f"ERROR: Paywall NOT reached for {slug}")
            sys.exit(1)

        # Проверка файловой структуры
        res_dir = Path(entry["path"])
        if not res_dir.exists() or not res_dir.is_dir():
            print(f"ERROR: Directory not found: {res_dir}")
            sys.exit(1)

        # Проверка наличия log.txt
        if not (res_dir / "log.txt").exists():
            print(f"ERROR: log.txt missing in {res_dir}")
            sys.exit(1)

        # Проверка скриншотов
        png_files = list(res_dir.glob("*.png"))
        if not png_files:
            print(f"ERROR: No PNG screenshots in {res_dir}")
            sys.exit(1)

        # Проверка наличия скриншота paywall
        paywall_pngs = [f for f in png_files if "paywall" in f.name.lower()]
        if not paywall_pngs:
            print(f"ERROR: No paywall screenshot found in {res_dir}")
            sys.exit(1)

        print(f"OK: {slug} validated (steps: {entry['steps_total']})")

    print("\nSMOKE TEST PASSED")
    sys.exit(0)

if __name__ == "__main__":
    run_smoke_test()
