"""
店舗物件自動収集ツール — メインエントリ

使い方:
  python main.py           # 即時実行
  python main.py --config custom.json
"""

import json
import logging
import os
import re
import sys
import io
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def _normalize_dedupe_address(address: str) -> str:
    text = address or ""
    text = re.sub(r"\s+", "", text)
    text = text.replace("神奈川県", "")
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    kanji_nums = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    text = re.sub(
        r"([一二三四五六七八九十])丁目",
        lambda m: kanji_nums.get(m.group(1), m.group(1)) + "丁目",
        text,
    )
    for old, new in [
        ("－", "-"),
        ("ー", "-"),
        ("―", "-"),
        ("丁目", "-"),
        ("番地", "-"),
        ("番", "-"),
        ("号", ""),
    ]:
        text = text.replace(old, new)
    return text


def _station_token(prop) -> str:
    text = " ".join([
        prop.nearest_station or "",
        prop.name or "",
        prop.address or "",
    ])
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"([一-龥ぁ-んァ-ンA-Za-z0-9・ヶ]+駅)", text)
    return m.group(1) if m else ""


def _image_dedupe_token(image_url: str) -> str:
    if not image_url:
        return ""
    url = unquote(image_url).lower()
    if any(x in url for x in ["logo", "noimage", "no_image", "common", "blank", "dummy"]):
        return ""
    path = urlparse(url).path
    name = Path(path).name
    if not name or "." not in name:
        return ""
    return name


def _area_close(a, b, tolerance: float = 3.0) -> bool:
    if not a or not b:
        return True
    return abs(float(a) - float(b)) <= tolerance


def remove_duplicate_properties(properties: list, status_map: dict, notes_map: dict, logger) -> list:
    """重複物件を1件にまとめる。調査情報があるものを優先して残す。"""
    parent = list(range(len(properties)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    exact_groups: dict[tuple[str, int], list[int]] = {}
    image_groups: dict[tuple[str, int], list[int]] = {}
    station_groups: dict[tuple[str, int, int, int], list[int]] = {}
    normalized_addresses: list[str] = []
    station_tokens: list[str] = []

    for i, prop in enumerate(properties):
        addr = _normalize_dedupe_address(prop.address)
        normalized_addresses.append(addr)
        station = _station_token(prop)
        station_tokens.append(station)
        if addr and prop.rent:
            exact_groups.setdefault((addr, prop.rent), []).append(i)

        img = _image_dedupe_token(prop.image_url)
        if img and prop.rent:
            image_groups.setdefault((img, prop.rent), []).append(i)

        if station and prop.rent and prop.area and prop.walk_minutes:
            area_bucket = round(float(prop.area))
            station_groups.setdefault((station, prop.rent, area_bucket, prop.walk_minutes), []).append(i)

    for indexes in exact_groups.values():
        for i in indexes[1:]:
            union(indexes[0], i)

    for indexes in image_groups.values():
        base = indexes[0]
        for i in indexes[1:]:
            if _area_close(properties[base].area, properties[i].area):
                union(base, i)

    for indexes in station_groups.values():
        base = indexes[0]
        for i in indexes[1:]:
            if _area_close(properties[base].area, properties[i].area):
                union(base, i)

    # 住所が途中までしかないケース。短い住所が長い住所の先頭と一致し、賃料も同じなら重複候補。
    for i in range(len(properties)):
        pi = properties[i]
        ai = normalized_addresses[i]
        if not ai or not pi.rent:
            continue
        for j in range(i + 1, len(properties)):
            pj = properties[j]
            if pi.rent != pj.rent:
                continue
            aj = normalized_addresses[j]
            if not aj:
                continue
            short, long = sorted([ai, aj], key=len)
            if len(short) >= 7 and long.startswith(short) and _area_close(pi.area, pj.area):
                union(i, j)
                continue
            if station_tokens[i] and station_tokens[i] == station_tokens[j]:
                if _area_close(pi.area, pj.area) and pi.walk_minutes == pj.walk_minutes:
                    union(i, j)

    status_score = {
        "検討中": 50,
        "調査中": 40,
        "見送り": 30,
        "未調査": 10,
    }

    def score(prop) -> tuple:
        key = prop.unique_key
        return (
            status_score.get(status_map.get(key, ""), 0),
            20 if notes_map.get(key) else 0,
            8 if prop.area else 0,
            4 if prop.floor else 0,
            2 if prop.walk_minutes else 0,
            len(prop.address or ""),
            len(prop.name or ""),
        )

    groups: dict[int, list] = {}
    for i, prop in enumerate(properties):
        groups.setdefault(find(i), []).append(prop)

    unique = []
    removed = 0
    duplicate_groups = 0
    for props in groups.values():
        if len(props) == 1:
            unique.append(props[0])
            continue
        duplicate_groups += 1
        props_sorted = sorted(props, key=score, reverse=True)
        keep = props_sorted[0]
        unique.append(keep)
        removed += len(props_sorted) - 1

    if removed:
        logger.info(f"重複除外: {removed} 件（{duplicate_groups} グループ / 住所+賃料・途中住所・画像URL）")
    return unique


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

    notes_path = BASE_DIR / "data" / "notes.json"
    notes_map: dict = {}
    if notes_path.exists():
        with open(notes_path, encoding="utf-8") as f:
            notes_map = json.load(f)
        logger.info(f"メモ読み込み: {len(notes_map)} 件")

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

    properties = remove_duplicate_properties(properties, status_map, notes_map, logger)

    # データ保存
    save_properties(properties, data_path)
    logger.info(f"データ保存完了: {data_path}")

    # HTMLレポート生成
    from report import generate_report
    generate_report(properties, config, str(report_path), prev_keys, status_map, notes_map)
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
