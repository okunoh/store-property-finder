"""
店舗物件自動収集ツール — メインエントリ

使い方:
  python main.py           # 即時実行
  python main.py --config custom.json
"""

import json
import logging
import os
import sys
import io
from datetime import datetime
from pathlib import Path

# Windowsコンソールのcp932エラーを回避
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_previous_keys(data_path: Path) -> set:
    if not data_path.exists():
        return set()
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    return {f"{p['site']}:{p['property_id']}" for p in data}


def save_properties(properties: list, data_path: Path) -> None:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in properties], f, ensure_ascii=False, indent=2)


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d')}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    # コマンドライン引数でconfigパス指定可能
    config_path = BASE_DIR / "config.json"
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--config" and i + 2 <= len(sys.argv) - 1:
            config_path = Path(sys.argv[i + 2])

    if not config_path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    output_cfg = config.get("output", {})
    report_path = BASE_DIR / output_cfg.get("report_path", "output/report.html")
    data_path = BASE_DIR / output_cfg.get("data_path", "data/properties.json")

    report_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 50)
    logger.info("店舗物件収集 開始")
    logger.info(f"対象エリア: {config['search']['areas']}")
    logger.info("=" * 50)

    # 前回データ読み込み（新着判定用）
    prev_keys = load_previous_keys(data_path)
    logger.info(f"前回データ: {len(prev_keys)} 件")

    # ステータス読み込み
    status_path = BASE_DIR / "data" / "status.json"
    status_map: dict = {}
    if status_path.exists():
        with open(status_path, encoding="utf-8") as f:
            status_map = json.load(f)
        logger.info(f"ステータス読み込み: {len(status_map)} 件")

    # スクレイピング実行
    from scraper import run_scraper
    properties = run_scraper(config)

    if not properties:
        logger.warning("物件が1件も取得できませんでした。サイト構造が変わった可能性があります。")

    # 削除済み物件を除外
    deleted_keys = {k for k, v in status_map.items() if v == "削除"}
    if deleted_keys:
        before = len(properties)
        properties = [p for p in properties if p.unique_key not in deleted_keys]
        logger.info(f"削除済み除外: {before - len(properties)} 件")

    # データ保存
    save_properties(properties, data_path)
    logger.info(f"データ保存完了: {data_path}")

    # HTMLレポート生成
    from report import generate_report
    generate_report(properties, config, str(report_path), prev_keys, status_map)
    logger.info(f"レポート生成完了: {report_path}")

    new_count = sum(1 for p in properties if p.is_new)
    logger.info(f"完了 - 総件数: {len(properties)}, 新着: {new_count}")
    logger.info(f"レポートを開く: {report_path.resolve()}")

    # GitHub Pages へ自動デプロイ
    try:
        from deploy import deploy
        deploy()
    except Exception as e:
        logger.warning(f"デプロイスキップ: {e}")


if __name__ == "__main__":
    main()
