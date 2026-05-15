"""
店舗物件スクレイパー — 10サイト対応 Playwright版
"""

import re
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── temponw 区コード（川崎市・横浜市） ──────────────────
TEMPONW_YOKOHAMA = list(range(101, 119))        # 101〜118
TEMPONW_KAWASAKI = [131, 132, 133, 134, 135, 136, 137]

# ── 対象エリア文字列（アドレスフィルタ用） ───────────────
TARGET_AREAS = ["川崎", "横浜"]


# ═══════════════════════════════════════════════════════
# データクラス
# ═══════════════════════════════════════════════════════
@dataclass
class Property:
    site: str
    property_id: str
    name: str
    address: str
    rent: Optional[int]
    area: Optional[float]
    walk_minutes: Optional[int]
    nearest_station: str
    floor: str
    url: str
    image_url: str = ""
    description: str = ""
    is_new: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def unique_key(self) -> str:
        return f"{self.site}:{self.property_id}"


# ═══════════════════════════════════════════════════════
# パーサユーティリティ
# ═══════════════════════════════════════════════════════
def _parse_rent(text: str) -> Optional[int]:
    if not text:
        return None
    text = text.replace(",", "").replace(" ", "").replace("　", "")
    m = re.search(r"([\d.]+)\s*万", text)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"(\d+)\s*円", text)
    if m:
        return int(m.group(1))
    return None


def _parse_area(text: str) -> Optional[float]:
    if not text:
        return None
    # 坪 → ㎡ 変換（1坪 = 3.30579㎡）
    m = re.search(r"([\d.]+)\s*坪", text)
    if m:
        return round(float(m.group(1)) * 3.30579, 2)
    m = re.search(r"([\d.]+)\s*[m㎡]", text)
    if m:
        return float(m.group(1))
    return None


def _parse_walk(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)\s*分", text)
    if m:
        return int(m.group(1))
    return None


def _in_target_area(text: str) -> bool:
    return any(a in text for a in TARGET_AREAS)


def _is_first_floor(floor: str) -> bool:
    """1F（路面階）かどうか判定。階数不明の場合は True（除外しない）。"""
    if not floor:
        return True
    f = floor.upper()
    # 全角→半角
    f = f.translate(str.maketrans("０１２３４５６７８９Ｆ", "0123456789F"))
    f = f.strip()
    # "1F", "1階", "1・2F", "1/2F" など1から始まる → OK（1・2F は1Fアクセスあり）
    return bool(re.match(r"^1[F階・/]", f)) or f in ("1F", "1階")


def _matches_filter(prop: "Property", cfg: dict, area_trusted: bool = False) -> bool:
    # エリアフィルタ（URL で既にエリア絞り込み済みの場合はスキップ可）
    if not area_trusted:
        area_text = f"{prop.address} {prop.nearest_station} {prop.name}"
        if not _in_target_area(area_text):
            return False
    if cfg.get("rent_max") and prop.rent and prop.rent > cfg["rent_max"]:
        return False
    if cfg.get("area_min") and prop.area and prop.area < cfg["area_min"]:
        return False
    if cfg.get("area_max") and prop.area and prop.area > cfg["area_max"]:
        return False
    if cfg.get("walk_minutes_max") and prop.walk_minutes and prop.walk_minutes > cfg["walk_minutes_max"]:
        return False
    if cfg.get("floor_1f_only") and prop.floor and not _is_first_floor(prop.floor):
        return False
    return True


# ═══════════════════════════════════════════════════════
# Playwright ページ取得ヘルパー
# ═══════════════════════════════════════════════════════
def _load(page, url: str, wait_sel: str = None, wait_ms: int = 4000) -> str:
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if wait_sel:
            try:
                page.wait_for_selector(wait_sel, timeout=8000)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
        return page.content()
    except Exception as e:
        logger.warning(f"ページ取得失敗 {url}: {e}")
        return ""


def _new_page(browser):
    return browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="ja-JP",
        viewport={"width": 1280, "height": 900},
    ).new_page()


def _img_src(img) -> str:
    return img.get("src", "") if img else ""


def _extract_text_prop(item, site: str, prop_id: str, url: str, image_url: str = "", area_hint: str = "") -> Optional[Property]:
    """テキスト全体からプロパティ情報を抽出する汎用パーサー"""
    text = item.get_text(" ", strip=True) if hasattr(item, "get_text") else str(item)
    text = re.sub(r"\s+", " ", text)
    # HTMLレンダリングで分割された数値を結合（例: "32. 95" → "32.95"）
    text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)

    # 賃料（万円 or 円）
    rent = None
    for pattern in [r"賃料[^\d]*([\d,.]+)万円", r"([\d,.]+)\s*万円", r"賃料[^\d]*([\d,]+)\s*円"]:
        m = re.search(pattern, text)
        if m:
            val = m.group(1).replace(",", "")
            if "万" in pattern or "万" in text[max(0, m.start()-5):m.end()]:
                rent = int(float(val) * 10000)
            else:
                rent = int(val)
            break

    # 面積（坪 or ㎡）
    area = None
    for pattern in [
        r"面積[^\d]*([\d.]+)\s*坪",    # "面積 XX坪"
        r"([\d.]+)\s*坪\s*[（(]",      # "XX坪（ or XX坪("
        r"面積[^\d]*([\d.]+)\s*[m㎡]", # "面積 XX.XXm²"
        r"([\d.]+)\s*㎡",              # "XX.XX㎡" 単体
        r"([\d.]+)\s*坪(?!\s*単)",     # "XX坪" 単体（坪単価は除外）
    ]:
        m = re.search(pattern, text)
        if m:
            val = float(m.group(1))
            if "坪" in pattern:
                area = round(val * 3.30579, 2)
            else:
                area = val
            if area > 5:
                break
            area = None

    # 駅徒歩
    walk = None
    station_text = ""
    for pattern in [r"(.{1,15}駅)\s*徒歩\s*(\d+)\s*分", r"(.{1,15}駅)\s*(\d+)\s*分"]:
        m = re.search(pattern, text)
        if m:
            station_text = m.group(0)
            walk = int(m.group(2))
            break
    if walk is None:
        m = re.search(r"徒歩\s*(\d+)\s*分", text)
        if m:
            walk = int(m.group(1))

    # 住所（神奈川県知事...免許番号は除外）
    address = ""
    m = re.search(r"(神奈川県(?!知事)[^\s　]{3,40})", text)
    if not m:
        # 市区だけの場合（横浜市XXX区YYY or 川崎市XXX区YYY）
        m = re.search(r"((?:横浜市|川崎市)[^\s　]{3,35})", text)
    if m:
        address = m.group(1)

    # 階数（例: "1F", "2F", "B1F", "1階", "地下1階", "所在階 2F"）
    floor = ""
    for fp in [
        r"所在階[^\d０-９B]*([BbＢｂ]?地下[０-９\d]+[FfＦｆ階]?|[BbＢｂ]?[０-９\d]+[FfＦｆ階])",
        r"(地下[０-９\d]+[FfＦｆ階]?|[BbＢｂ][０-９\d]+[FfＦｆ]|[１２３４５６７８９1-9][０-９\d]*(?:[・/][０-９\d]*)?[FfＦｆ階])",
    ]:
        fm = re.search(fp, text)
        if fm:
            floor = fm.group(1)
            break

    return Property(
        site=site,
        property_id=prop_id,
        name=text[:50],
        address=address or area_hint,
        rent=rent,
        area=area,
        walk_minutes=walk,
        nearest_station=station_text,
        floor=floor,
        url=url,
        image_url=image_url,
    )


# ═══════════════════════════════════════════════════════
# 1. アットホーム
# ═══════════════════════════════════════════════════════
def scrape_athome(cfg: dict, browser) -> list[Property]:
    # 店舗 + 事務所 の両セクションをスクレイプ
    SECTION_URLS = {
        "川崎市": [
            ("https://www.athome.co.jp/rent_store/kanagawa/kawasaki-locate/list/",
             re.compile(r"/rent_store/\d+")),
            ("https://www.athome.co.jp/chintai/jimusho/kanagawa/kawasaki-mcity/list/",
             re.compile(r"/chintai/b-\d+|/jimusho/\d+")),
        ],
        "横浜市": [
            ("https://www.athome.co.jp/rent_store/kanagawa/yokohama-locate/list/",
             re.compile(r"/rent_store/\d+")),
            ("https://www.athome.co.jp/chintai/jimusho/kanagawa/yokohama-mcity/list/",
             re.compile(r"/chintai/b-\d+|/jimusho/\d+")),
        ],
    }
    results = []
    page = _new_page(browser)
    try:
        for area, sections in SECTION_URLS.items():
            if area not in cfg["areas"]:
                continue
            for base_url, link_pat in sections:
                for pg in range(1, 4):
                    url = base_url if pg == 1 else base_url.rstrip("/") + f"/page{pg}/"
                    html = _load(page, url, wait_sel=".area-inner", wait_ms=6000)
                    if len(html) < 30000 or "認証にご協力" in html:
                        logger.warning(f"アットホーム: ボット認証検出 ({url[:60]}...)")
                        break
                    soup = BeautifulSoup(html, "lxml")
                    items = soup.find_all(class_="area-inner")
                    if not items:
                        break
                    for item in items:
                        a = item.find("a", href=link_pat)
                        if not a:
                            # フォールバック: 任意のアットホーム物件リンク
                            a = item.find("a", href=re.compile(r"/rent_store/\d+|/chintai/b-\d+"))
                        if not a:
                            continue
                        href = a["href"].split("?")[0]  # クエリパラム除去
                        prop_id = re.search(r"/(\d{10,})", href)
                        prop_id = prop_id.group(1) if prop_id else href
                        url_full = f"https://www.athome.co.jp{href}" if href.startswith("/") else href
                        img = item.select_one("img[src*='athome.co.jp']")
                        prop = _extract_text_prop(item, "アットホーム", prop_id, url_full,
                                                  _img_src(img), area_hint=area)
                        if prop and _matches_filter(prop, cfg, area_trusted=True):
                            results.append(prop)
                    time.sleep(1)
    finally:
        page.context.close()
    logger.info(f"アットホーム: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 2. HOMES
# ═══════════════════════════════════════════════════════
def scrape_homes(cfg: dict, browser) -> list[Property]:
    URLS = {
        "川崎市": "https://www.homes.co.jp/chintai/tempo/kanagawa/kawasaki-mcity/list/",
        "横浜市": "https://www.homes.co.jp/chintai/tempo/kanagawa/yokohama-mcity/list/",
    }
    results = []
    page = _new_page(browser)
    try:
        for area, base_url in URLS.items():
            if area not in cfg["areas"]:
                continue
            html = _load(page, base_url, wait_ms=5000)
            soup = BeautifulSoup(html, "lxml")
            for a in soup.select("a[href*='/chintai/b-']"):
                href = a["href"]
                m = re.search(r"/b-(\w+)/", href)
                if not m:
                    continue
                prop_id = m.group(1)
                url_full = f"https://www.homes.co.jp{href}" if href.startswith("/") else href
                parent = a.find_parent("div") or a.find_parent("li")
                if not parent:
                    continue
                img = parent.select_one("img[src*='http']")
                prop = _extract_text_prop(parent, "HOMES", prop_id, url_full,
                                          _img_src(img), area_hint=area)
                if prop and _matches_filter(prop, cfg, area_trusted=True):
                    results.append(prop)
    finally:
        page.context.close()
    logger.info(f"HOMES: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 3. テンポスマート
# ═══════════════════════════════════════════════════════
def scrape_temposmart(cfg: dict, browser) -> list[Property]:
    BASE = "https://www.temposmart.jp/estates/pref/14"
    results = []
    page = _new_page(browser)
    try:
        for pg in range(1, 6):
            url = BASE if pg == 1 else f"{BASE}?page={pg}"
            html = _load(page, url, wait_ms=4000)
            soup = BeautifulSoup(html, "lxml")
            links = list(dict.fromkeys(
                a["href"] for a in soup.select("a[href]")
                if re.search(r"/estates/\d+$", a["href"])
            ))
            if not links:
                break
            for link in links:
                prop_id = re.search(r"/estates/(\d+)$", link).group(1)
                # カードを探す
                a_el = soup.select_one(f"a[href='{link}']")
                card = a_el.find_parent("li") or a_el.find_parent("div") if a_el else None
                img = card.select_one("img[src]") if card else None
                prop = _extract_text_prop(card or a_el, "テンポスマート", prop_id, link,
                                          _img_src(img))
                if prop and _matches_filter(prop, cfg):
                    results.append(prop)
            time.sleep(1.5)
    finally:
        page.context.close()
    logger.info(f"テンポスマート: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 4. 飲食店ドットコム
# ═══════════════════════════════════════════════════════
def scrape_inshokuten(cfg: dict, browser) -> list[Property]:
    BASE = "https://www.inshokuten.com/bukken/kanto/bukkens/list/local-yokohama_kawasaki/"
    results = []
    page = _new_page(browser)
    try:
        for pg in range(1, 5):
            url = BASE if pg == 1 else f"{BASE}?page={pg}"
            html = _load(page, url, wait_sel=".bukkenItem", wait_ms=5000)
            soup = BeautifulSoup(html, "lxml")
            items = soup.select(".bukkenItem")
            if not items:
                break
            for item in items:
                a = item.select_one("a[href*='/bukken/bukkens/']")
                if not a:
                    continue
                href = a["href"]
                m = re.search(r"/bukken/bukkens/(\d+)", href)
                prop_id = m.group(1) if m else href
                url_full = f"https://www.inshokuten.com{href}" if href.startswith("/") else href
                img = item.select_one("img[src*='http']")
                prop = _extract_text_prop(item, "飲食店ドットコム", prop_id, url_full,
                                          _img_src(img))
                if prop and _matches_filter(prop, cfg, area_trusted=True):
                    results.append(prop)
            time.sleep(1.5)
    finally:
        page.context.close()
    logger.info(f"飲食店ドットコム: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 5. 店舗そのままオークション (sonomama)
# ═══════════════════════════════════════════════════════
def scrape_sonomama(cfg: dict, browser) -> list[Property]:
    BASE = "https://sonomama.net/app/?action=public_property_list_search&pref=14"
    results = []
    page = _new_page(browser)
    try:
        for pg in range(1, 5):
            url = f"{BASE}&page_index={pg}"
            html = _load(page, url, wait_ms=5000)
            soup = BeautifulSoup(html, "lxml")
            rows = soup.select("div.list_row")
            if not rows:
                break
            for row in rows:
                a = row.find("a", href=re.compile(r"property_detail&pid=(\d+)"))
                if not a:
                    continue
                href = a["href"]
                m = re.search(r"pid=(\d+)", href)
                prop_id = m.group(1) if m else href
                url_full = f"https://sonomama.net{href}" if href.startswith("/") else href
                img = row.select_one("div.item_img img, img[src*='http']")
                prop = _extract_text_prop(row, "店舗そのままオークション", prop_id, url_full,
                                          _img_src(img))
                if prop and _matches_filter(prop, cfg):
                    results.append(prop)
            time.sleep(1.5)
    finally:
        page.context.close()
    logger.info(f"店舗そのままオークション: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 6. テナント.biz
# ═══════════════════════════════════════════════════════
def scrape_tenanto_biz(cfg: dict, browser) -> list[Property]:
    AREA_CODES = {"川崎市": "14130", "横浜市": "14100"}
    results = []
    page = _new_page(browser)
    try:
        for area in cfg["areas"]:
            code = AREA_CODES.get(area)
            if not code:
                continue
            for pg in range(1, 4):
                url = f"https://www.tenanto-office.biz/list.php?ma={code}&p={pg}"
                html = _load(page, url, wait_ms=3000)
                soup = BeautifulSoup(html, "lxml")
                links = list(dict.fromkeys(
                    a["href"] for a in soup.select("a[href*='detail.php?id=']")
                ))
                if not links:
                    break
                for href in links:
                    m = re.search(r"id=(\d+)", href)
                    prop_id = m.group(1) if m else href
                    url_full = f"https://www.tenanto-office.biz/{href.lstrip('/')}"
                    a_el = soup.select_one(f"a[href='{href}']")
                    parent = a_el.find_parent("tr") or a_el.find_parent("div") if a_el else None
                    img = parent.select_one("img[src]") if parent else None
                    prop = _extract_text_prop(parent or a_el, "テナント.biz", prop_id, url_full,
                                              _img_src(img), area_hint=area)
                    if prop and _matches_filter(prop, cfg, area_trusted=True):
                        results.append(prop)
                time.sleep(1)
    finally:
        page.context.close()
    logger.info(f"テナント.biz: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 7. テナントショップ
# ═══════════════════════════════════════════════════════
def scrape_tenantshop(cfg: dict, browser) -> list[Property]:
    results = []
    page = _new_page(browser)
    try:
        seen_hrefs: set[str] = set()
        for pg in range(1, 5):
            url = (
                "https://www.tenant-shop.com/chintai_tenpo/pa-15/"
                if pg == 1
                else f"https://www.tenant-shop.com/index.php?ac=2&c=12&pa=15&p={pg}"
            )
            html = _load(page, url, wait_ms=4000)
            soup = BeautifulSoup(html, "lxml")
            detail_hrefs = list(dict.fromkeys(
                a["href"] for a in soup.select("a[href*='/detail/e-']")
            ))
            if not detail_hrefs:
                break
            for href in detail_hrefs:
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                prop_id = re.search(r"/detail/(e-[\w-]+)/", href)
                prop_id = prop_id.group(1) if prop_id else href
                url_full = f"https://www.tenant-shop.com{href}"
                d_html = _load(page, url_full, wait_ms=3000)
                if not d_html:
                    continue
                d_soup = BeautifulSoup(d_html, "lxml")
                body = d_soup.find("body")
                if not body:
                    continue
                img = body.select_one("img[src*='photo'], img[src*='image']")
                prop = _extract_text_prop(body, "テナントショップ", prop_id, url_full, _img_src(img))
                if prop and _matches_filter(prop, cfg):
                    results.append(prop)
                time.sleep(1.5)
            time.sleep(1)
    finally:
        page.context.close()
    logger.info(f"テナントショップ: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 8. テンポダス (Tempodas / SUUMO店舗)
# ═══════════════════════════════════════════════════════
def scrape_tempodas(cfg: dict, browser) -> list[Property]:
    BASE = "https://tempodas.com/search/area/kanagawa"
    results = []
    page = _new_page(browser)
    try:
        seen_urls: set[str] = set()
        for pg in range(1, 4):
            url = BASE if pg == 1 else f"{BASE}?page={pg}"
            html = _load(page, url, wait_ms=5000)
            soup = BeautifulSoup(html, "lxml")
            detail_urls = list(dict.fromkeys(
                ("https://tempodas.com" + a["href"]) if a["href"].startswith("/") else a["href"]
                for a in soup.select("a[href]")
                if re.search(r"/search/detail/\d+/", a["href"])
            ))
            if not detail_urls:
                break
            for url_full in detail_urls:
                if url_full in seen_urls:
                    continue
                seen_urls.add(url_full)
                m = re.search(r"/search/detail/(\d+)/", url_full)
                prop_id = m.group(1) if m else url_full
                d_html = _load(page, url_full, wait_ms=4000)
                if not d_html:
                    continue
                d_soup = BeautifulSoup(d_html, "lxml")
                body = d_soup.find("body")
                if not body:
                    continue
                img = body.select_one("img[src*='photo'], img[src*='thumbnail'], img[src*='image']")
                prop = _extract_text_prop(body, "テンポダス", prop_id, url_full, _img_src(img))
                if prop and _matches_filter(prop, cfg):
                    results.append(prop)
                time.sleep(1.5)
            time.sleep(1)
    finally:
        page.context.close()
    logger.info(f"テンポダス: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 9. 居抜き市場 (inuki-ichiba) ※居抜き情報.COM 代替
# ═══════════════════════════════════════════════════════
def scrape_inuki_ichiba(cfg: dict, browser) -> list[Property]:
    BASE = "https://inuki-ichiba.jp/rent/kanagawaken"
    results = []
    page = _new_page(browser)
    try:
        for pg in range(1, 5):
            url = BASE if pg == 1 else f"{BASE}?page={pg}"
            html = _load(page, url, wait_ms=4000)
            soup = BeautifulSoup(html, "lxml")
            links = list(dict.fromkeys(
                a["href"] for a in soup.select("a[href]")
                if re.search(r"^/rent/\d+$", a["href"])
            ))
            if not links:
                break
            for href in links:
                prop_id = href.split("/")[-1]
                url_full = f"https://inuki-ichiba.jp{href}"
                a_el = soup.select_one(f"a[href='{href}']")
                parent = a_el.find_parent("div") or a_el.find_parent("li") if a_el else None
                img = parent.select_one("img[src]") if parent else None
                prop = _extract_text_prop(parent or a_el, "居抜き市場", prop_id, url_full,
                                          _img_src(img))
                if prop and _matches_filter(prop, cfg):
                    results.append(prop)
            time.sleep(1.5)
    finally:
        page.context.close()
    logger.info(f"居抜き市場: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# 10. 店舗ネットワーク (temponw)
# ═══════════════════════════════════════════════════════
def scrape_temponw(cfg: dict, browser) -> list[Property]:
    """横浜市・川崎市の各区コードを使って物件一覧から取得"""
    codes = TEMPONW_YOKOHAMA + TEMPONW_KAWASAKI
    base = "https://www.temponw.com/area_search/result?prefectures_code=14&name_j=%E7%A5%9E%E5%A5%88%E5%B7%9D%E7%9C%8C"
    city_params = "&".join(f"cities%5B%5D={c}" for c in codes)
    search_url = f"{base}&{city_params}"

    results = []
    seen_ids: set[str] = set()
    page = _new_page(browser)
    try:
        for pg in range(1, 11):
            url = search_url if pg == 1 else f"{search_url}&page={pg}"
            html = _load(page, url, wait_ms=6000)
            if not html:
                break
            soup = BeautifulSoup(html, "lxml")

            # デスクトップ版の物件カード（sm-hidden = モバイルで非表示 = デスクトップ表示）
            cards = soup.select("div.result-contents.sm-hidden")
            if not cards:
                break

            for card in cards:
                # フォームから item_no を取得（プロパティの一意ID）
                form = card.find("form", action=re.compile(r"store_detail"))
                if not form:
                    continue
                item_inp = form.find("input", attrs={"name": "item_no"})
                src_inp = form.find("input", attrs={"name": "source_system_code"})
                if not item_inp:
                    continue
                item_no = item_inp["value"]
                if item_no in seen_ids:
                    continue
                seen_ids.add(item_no)
                src_code = src_inp["value"] if src_inp else "99"
                detail_url = (
                    f"https://www.temponw.com/store_detail"
                    f"?source_system_code={src_code}&item_no={item_no}&prefectures_code=14"
                )
                img = card.select_one("img[src*='http']")
                prop = _extract_text_prop(card, "店舗ネットワーク", item_no, detail_url, _img_src(img))
                if prop and _matches_filter(prop, cfg, area_trusted=True):
                    results.append(prop)

            time.sleep(2)
    finally:
        page.context.close()
    logger.info(f"店舗ネットワーク: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# メインエントリ
# ═══════════════════════════════════════════════════════
SCRAPERS = {
    "homes":        scrape_homes,
    "temposmart":   scrape_temposmart,
    "inshokuten":   scrape_inshokuten,
    "sonomama":     scrape_sonomama,
    "tenanto_biz":  scrape_tenanto_biz,
    "tenantshop":   scrape_tenantshop,
    "tempodas":     scrape_tempodas,
    "inuki_ichiba": scrape_inuki_ichiba,
    "temponw":      scrape_temponw,
    "athome":       scrape_athome,   # 最後に実行（セッションウォームアップ後がCAPTCHA回避しやすい）
}


def run_scraper(config: dict) -> list[Property]:
    from playwright.sync_api import sync_playwright

    cfg_search = config["search"]
    sites_cfg = config.get("sites", {})
    all_props: list[Property] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        for site_key, scraper_fn in SCRAPERS.items():
            if not sites_cfg.get(site_key, True):
                continue
            logger.info(f"── {site_key} 開始 ──")
            try:
                props = scraper_fn(cfg_search, browser)
                all_props.extend(props)
            except Exception as e:
                logger.error(f"{site_key} エラー: {e}")
        browser.close()

    # 重複除去（サイト+ID）
    seen = set()
    unique = []
    for p in all_props:
        if p.unique_key not in seen:
            seen.add(p.unique_key)
            unique.append(p)

    logger.info(f"合計 {len(unique)} 件（重複除去後）")
    return unique
