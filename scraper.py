"""
店舗物件スクレイパー — 10サイト対応 Playwright版
"""

import re
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# playwright-stealth（任意）
try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH = _Stealth(navigator_languages_override=("ja-JP", "ja"))
    _HAS_STEALTH = True
except ImportError:
    _STEALTH = None
    _HAS_STEALTH = False

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


def _new_page(browser, stealth: bool = False):
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="ja-JP",
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()
    # ── ボット検知回避: navigator.webdriver を隠す ──
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP','ja','en-US','en']});
        window.chrome = {runtime: {}};
    """)
    if stealth and _HAS_STEALTH:
        _STEALTH.apply_stealth_sync(page)
    return page


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
# 1. アットホーム — ヘルパー
# ═══════════════════════════════════════════════════════
def _athome_floor_from_name(name: str) -> str:
    """
    アットホーム一覧の物件名から所在階を抽出。
    例:
      "セントラルビル 地下1階の貸店舗"  → "地下1階"
      "司生堂ビル 2階の貸店舗・事務所"  → "2階"
      "第２アーバン東横 1階の貸店舗"    → "1階"
      "百合丘 地下1階/地上4階地下1階建" → "地下1階"
      "新塚越 6階/地上18階地下2階建"    → "6階"
      "ルリエ新川崎 6階の貸店舗"        → "6階"
      "松本ビル 2F"                     → "2F"
    """
    # パターン1: "X階/地上Y階" — 所在階/建物階建 形式
    m = re.search(r"(地下\s*\d+\s*[階FfＦｆ]|\d+\s*[FfＦｆ階])\s*/\s*(?:地上|地下)?\d+", name)
    if m:
        return m.group(1).replace(" ", "")
    # パターン2: "X階の貸〜" or "地下X階の貸〜"
    m = re.search(r"(地下\s*\d+\s*[FfＦｆ階]|\d+\s*[FfＦｆ階])\s*の貸", name)
    if m:
        return m.group(1).replace(" ", "")
    # パターン3: 末尾の "XF" or "X階"
    m = re.search(r"([BbＢｂ]?\d+[FfＦｆ階])\s*$", name)
    if m:
        return m.group(1)
    return ""


def _athome_detail_floor(soup) -> str:
    """
    アットホーム詳細ページから所在階を取得。
    th/dt ラベルまたはテキスト全体から抽出。
    """
    # th/dt → td/dd ペア
    for th in soup.select("th, dt"):
        label = th.get_text(strip=True)
        if label in ("所在階", "階数", "所在・利用階", "所在階数", "利用階"):
            td = th.find_next_sibling("td") or th.find_next_sibling("dd")
            if td:
                v = td.get_text(strip=True)
                if v:
                    return v
    # テキスト全体パターン
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = re.search(
        r"(?:所在階|所在・利用階|利用階|使用階)\s*[：:・]?\s*"
        r"(地下\s*\d+\s*[FfＦｆ階]?|建物全部|[BbＢｂ]?\d+\s*[FfＦｆ階])",
        text,
    )
    if m:
        return m.group(1).replace(" ", "")
    if "建物全部" in text:
        return "建物全部"
    return ""


# ═══════════════════════════════════════════════════════
# 1. アットホーム
# ═══════════════════════════════════════════════════════
def scrape_athome(cfg: dict, browser) -> list[Property]:
    """
    athome.co.jp/rent_store/ から川崎市・横浜市の店舗物件を収集。
    --disable-blink-features=AutomationControlled + stealth で CAPTCHA を回避。

    カード構造（li.card-box > a > table.area-inner__right > tr.tr-top > td[0..4]）:
      td[0]: 駅名 + 徒歩 + 住所
      td[1]: 賃料（万円）
      td[2]: 敷金/礼金
      td[3]: 面積（m²/坪）
      td[4]: 築年
    """
    AREA_URLS = {
        "川崎市": "https://www.athome.co.jp/rent_store/kanagawa/kawasaki-locate/list/",
        "横浜市": "https://www.athome.co.jp/rent_store/kanagawa/yokohama-locate/list/",
    }

    results = []
    seen_ids: set[str] = set()
    page = _new_page(browser, stealth=True)
    try:
        # ウォームアップ: トップページを先に訪問
        try:
            page.goto("https://www.athome.co.jp/", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, 500)")
            page.wait_for_timeout(1000)
        except Exception:
            pass

        for area, base_url in AREA_URLS.items():
            if area not in cfg["areas"]:
                continue
            for pg in range(1, 11):   # 最大10ページ（1ページ30件）
                url = base_url if pg == 1 else base_url.rstrip("/") + f"/page{pg}/"
                html = _load(page, url, wait_sel="li.card-box", wait_ms=8000)

                # CAPTCHA / ブロック検出
                logger.info(f"アットホーム: HTML {len(html)} bytes ({url[:60]}...)")
                if not html or len(html) < 50000:
                    logger.warning(f"アットホーム: ページ取得失敗 ({url[:60]}...)")
                    break
                if "認証にご協力" in html:
                    logger.warning(f"アットホーム: ボット認証検出 ({url[:60]}...)")
                    break

                soup = BeautifulSoup(html, "lxml")
                cards = soup.select("li.card-box")
                logger.info(f"アットホーム: li.card-box={len(cards)}, noindex={'noindex' in html[:500]}")
                if not cards:
                    # デバッグ用: HTMLをファイルに保存
                    _dbg = Path(__file__).parent / "logs" / "athome_debug.html"
                    _dbg.write_text(html[:200000], encoding="utf-8", errors="replace")
                    logger.warning(f"アットホーム: カード0件 → {_dbg} に保存 ({url[:60]})")
                    break

                for card in cards:
                    # 物件ID取得
                    inp = card.find("input", attrs={"name": "bukken-check[]"})
                    prop_id = inp["value"] if inp else None
                    if not prop_id:
                        a_link = card.select_one("a[href*='/rent_store/']")
                        if not a_link:
                            continue
                        m = re.search(r"/rent_store/(\d+)", a_link["href"])
                        prop_id = m.group(1) if m else None
                    if not prop_id or prop_id in seen_ids:
                        continue
                    seen_ids.add(prop_id)
                    url_full = f"https://www.athome.co.jp/rent_store/{prop_id}/"

                    # 画像
                    img = card.select_one("img[src*='athome.co.jp']")

                    # テーブルセルからデータ取得
                    tds = card.select("tr.tr-top > td")
                    rent = area_val = walk = None
                    station = address = ""

                    if len(tds) >= 4:
                        # td[0]: 駅名・徒歩・住所
                        td0_text = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True))
                        wm = re.search(r"(.{1,20}?駅)[^徒]*徒歩\s*(\d+)分", td0_text)
                        if wm:
                            station = wm.group(1)
                            walk = int(wm.group(2))
                        else:
                            wm2 = re.search(r"徒歩\s*(\d+)分", td0_text)
                            if wm2:
                                walk = int(wm2.group(1))
                        am_addr = re.search(r"((?:川崎市|横浜市)[^\s　]{3,35})", td0_text)
                        address = am_addr.group(1) if am_addr else ""

                        # td[1]: 賃料
                        td1_text = re.sub(r"\s+", "", tds[1].get_text(" ", strip=True))
                        rm = re.search(r"([\d.]+)万円", td1_text)
                        if rm:
                            rent = int(float(rm.group(1)) * 10000)

                        # td[3]: 面積
                        td3_text = tds[3].get_text(" ", strip=True)
                        am = re.search(r"([\d.]+)\s*m", td3_text)
                        if am:
                            area_val = float(am.group(1))
                        else:
                            am2 = re.search(r"([\d.]+)\s*坪", td3_text)
                            if am2:
                                area_val = round(float(am2.group(1)) * 3.30579, 2)

                    # タイトルから物件名 + 階数抽出
                    title_el = card.select_one(".area-title__text")
                    name = title_el.get_text(strip=True)[:50] if title_el else f"アットホーム {prop_id}"
                    floor = _athome_floor_from_name(name)

                    prop = Property(
                        site="アットホーム",
                        property_id=prop_id,
                        name=name,
                        address=address,
                        rent=rent,
                        area=area_val,
                        walk_minutes=walk,
                        nearest_station=station,
                        floor=floor,
                        url=url_full,
                        image_url=_img_src(img),
                    )

                    # 階数以外のフィルタで事前絞り込み
                    cfg_no_floor = {**cfg, "floor_1f_only": False}
                    if not _matches_filter(prop, cfg_no_floor, area_trusted=True):
                        continue

                    # 1Fフィルタ有効 かつ 名前から階数不明 → 詳細ページで確認
                    if cfg.get("floor_1f_only") and not prop.floor:
                        try:
                            d_html = _load(page, url_full, wait_ms=3000)
                            if d_html and "認証にご協力" not in d_html and len(d_html) > 10000:
                                d_soup = BeautifulSoup(d_html, "lxml")
                                detail_floor = _athome_detail_floor(d_soup)
                                if detail_floor:
                                    prop.floor = detail_floor
                                    logger.debug(f"アットホーム詳細: {prop_id} 所在階={detail_floor}")
                            else:
                                logger.debug(f"アットホーム詳細: {prop_id} CAPTCHA/取得失敗 → 階数不明扱い")
                        except Exception as e:
                            logger.debug(f"アットホーム詳細取得失敗 {prop_id}: {e}")
                        time.sleep(4)   # 詳細ページ間のウェイト

                    if _matches_filter(prop, cfg, area_trusted=True):
                        results.append(prop)

                # ページ送り: カードが30件未満なら最終ページ
                if len(cards) < 30:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
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
    seen_ids: set[str] = set()
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
                if prop_id in seen_ids:
                    continue
                # seen_ids への追加は面積取得成功後に行う（PRテザーカードをスキップするため）
                url_full = f"https://www.homes.co.jp{href}" if href.startswith("/") else href

                # 正しいカード要素を取得（div.moduleInner が物件カードの外枠）
                card = a.find_parent(class_=re.compile(r"moduleInner"))
                if not card:
                    card = a.find_parent("div") or a.find_parent("li")
                if not card:
                    continue

                # ── テーブル構造解析（階数・賃料・面積）──
                # HOMES一覧テーブル構造:
                #   tr: ['面積/坪数', '135.5m²/40.98坪']
                #   tr: ['', '階数', '部屋', '賃料/管理費等', '坪単価', ..., '面積', ...]  ← ヘッダー行
                #   tr: ['', '-',   '-',   '23万円/-',      ...,           '135.5m²', ...] ← データ行
                rent = area_val = None
                floor_hint = ""

                trs = card.select("tr")
                header_row_idx = None
                floor_col = rent_col = -1

                for i, tr in enumerate(trs):
                    cells = [td.get_text(strip=True) for td in tr.select("td,th")]
                    if not cells:
                        continue

                    # 「面積/坪数」行 → 面積取得（㎡ / m² / 坪）
                    if cells[0] and "面積" in cells[0] and len(cells) >= 2:
                        t = cells[1] if len(cells) > 1 else ""
                        # "135.5m²/40.98坪" or "135.5㎡" or "40.98坪"
                        am = re.search(r"([\d.]+)\s*(?:㎡|m[\S]?)", t)
                        if am:
                            area_val = float(am.group(1))
                        else:
                            am = re.search(r"([\d.]+)\s*坪(?!\s*単)", t)
                            if am:
                                area_val = round(float(am.group(1)) * 3.30579, 2)

                    # 「階数」がヘッダーにある行を検出
                    if "階数" in cells:
                        header_row_idx = i
                        floor_col = cells.index("階数")
                        rent_col = next(
                            (j for j, c in enumerate(cells) if "賃料" in c), -1
                        )

                    # ヘッダーの直後がデータ行
                    elif header_row_idx is not None and i == header_row_idx + 1:
                        if 0 <= floor_col < len(cells):
                            fv = cells[floor_col]
                            if fv and fv != "-":
                                floor_hint = fv          # "1階", "2階" など
                            elif fv == "-":
                                floor_hint = "不明"      # 明示的に不明 → 1Fフィルタで除外
                        if 0 <= rent_col < len(cells) and rent is None:
                            rm = re.search(r"([\d.]+)万円", cells[rent_col])
                            if rm:
                                rent = int(float(rm.group(1)) * 10000)
                        header_row_idx = None

                # 「建物全部」= 建物一棟丸ごと → 1Fとはみなさない
                card_text_full = card.get_text(" ", strip=True)
                if "建物全部" in card_text_full:
                    floor_hint = "建物全部"

                # 賃料フォールバック
                if rent is None:
                    rm = re.search(r"([\d.]+)\s*万円", card_text_full)
                    if rm:
                        rent = int(float(rm.group(1)) * 10000)

                # PRテザーカード（面積なし・階数なし）はスキップ
                # → 同じIDの完全カードが後に出てくるので seen_ids に入れず再処理させる
                if area_val is None and not floor_hint:
                    continue  # seen_ids には追加しない

                # 完全カード確認済み → seen_ids に登録
                seen_ids.add(prop_id)

                # ── 残りは汎用抽出 ──
                img = card.select_one("img[src*='http']")
                prop = _extract_text_prop(card, "HOMES", prop_id, url_full,
                                          _img_src(img), area_hint=area)
                if prop:
                    if rent is not None:
                        prop.rent = rent
                    if area_val is not None:
                        prop.area = area_val
                    if floor_hint:          # テーブル解析結果を優先
                        prop.floor = floor_hint
                    if _matches_filter(prop, cfg, area_trusted=True):
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
                a_els = soup.select("a[href*='detail.php?id=']")
                if not a_els:
                    break
                seen_hrefs: set[str] = set()
                for a_el in a_els:
                    href = a_el["href"]
                    if href in seen_hrefs:
                        continue
                    seen_hrefs.add(href)
                    m = re.search(r"id=(\d+)", href)
                    prop_id = m.group(1) if m else href
                    url_full = f"https://www.tenanto-office.biz/{href.lstrip('/')}"
                    parent = a_el.find_parent("tr") or a_el.find_parent("div")

                    # ── td位置指定パース ──
                    # 列順: [空] [駅名] [徒歩] [住所] [所在階X/Y] [面積+坪単価] [総賃料] [築年] ...
                    tds = parent.find_all("td") if parent else []
                    prop = None
                    if len(tds) >= 7:
                        station_t = tds[1].get_text(strip=True)
                        walk_t    = tds[2].get_text(strip=True)   # "-5" → 5分
                        addr_t    = tds[3].get_text(strip=True)
                        floor_t   = tds[4].get_text(strip=True)   # "3/8"
                        area_t    = tds[5].get_text(strip=True)   # "52坪8,462"
                        rent_t    = tds[6].get_text(strip=True)   # "440,000"

                        # 徒歩
                        wm = re.search(r"(\d+)", walk_t)
                        walk = int(wm.group(1)) if wm else None

                        # 所在階: "X/Y" → 1F判定用に "X階"
                        fm = re.match(r"(\d+)/\d+", floor_t)
                        floor = (fm.group(1) + "階") if fm else ""

                        # 面積 (坪)
                        am = re.search(r"([\d.]+)坪", area_t)
                        area_val = round(float(am.group(1)) * 3.30579, 2) if am else None

                        # 総賃料 (円): "440,000" → 440000
                        rent_clean = rent_t.replace(",", "").replace("\xa0", "")
                        if rent_clean.isdigit():
                            rent = int(rent_clean)
                        else:
                            rm2 = re.search(r"([\d.]+)\s*万", rent_t)
                            rent = int(float(rm2.group(1)) * 10000) if rm2 else None

                        prop = Property(
                            site="テナント.biz",
                            property_id=prop_id,
                            name=f"{station_t} {addr_t}"[:50],
                            address=addr_t,
                            rent=rent,
                            area=area_val,
                            walk_minutes=walk,
                            nearest_station=station_t,
                            floor=floor,
                            url=url_full,
                        )
                    else:
                        img = parent.select_one("img[src]") if parent else None
                        prop = _extract_text_prop(parent or a_el, "テナント.biz", prop_id,
                                                  url_full, _img_src(img), area_hint=area)  # type: ignore[arg-type]

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
    seen_ids: set[str] = set()
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
                if prop_id in seen_ids:
                    continue
                seen_ids.add(prop_id)
                url_full = f"https://inuki-ichiba.jp{href}"

                # ── 一覧カードから駅・住所を取得 ──
                a_el = soup.select_one(f"a[href='{href}']")
                card = a_el.find_parent("div") or a_el.find_parent("li") if a_el else None
                card_text = re.sub(r"\s+", " ", card.get_text(" ", strip=True)) if card else ""

                # 徒歩・駅名: "石川町駅 | 徒歩2分 | ..."
                walk, station = None, ""
                wm = re.search(r"(.{1,15}駅)\s*[|｜]?\s*徒歩\s*(\d+)分", card_text)
                if wm:
                    station = wm.group(1)
                    walk = int(wm.group(2))

                # 住所
                addr_m = re.search(r"((?:横浜市|川崎市)[^\s　]{3,35})", card_text)
                address = addr_m.group(1) if addr_m else ""

                # ── 詳細ページから賃料・階数・面積を取得 ──
                d_html = _load(page, url_full, wait_ms=2000)
                if not d_html:
                    continue
                d_text = re.sub(r"\s+", " ",
                                BeautifulSoup(d_html, "lxml").get_text(" ", strip=True))

                # 賃料: "賃料 264,000 円(税込)"
                rent = None
                rm = re.search(r"賃料\s*([\d,]+)\s*円", d_text)
                if rm:
                    rent = int(rm.group(1).replace(",", ""))

                # 階数/面積: "1F / 14.03坪 (46.38㎡)" or "B1F / 40坪"
                floor, area_val = "", None
                fm = re.search(
                    r"階数/面積\s*([BbＢｂ地下]?\d+F)\s*/\s*([\d.]+)\s*坪",
                    d_text, re.IGNORECASE
                )
                if fm:
                    floor = fm.group(1).upper()
                    area_val = round(float(fm.group(2)) * 3.30579, 2)
                else:
                    # フォールバック: "1F/40坪" や "2F/80㎡" など
                    fm2 = re.search(
                        r"([BbＢｂ地下]?\d+F)\s*/\s*([\d.]+)\s*(坪|㎡)",
                        d_text, re.IGNORECASE
                    )
                    if fm2:
                        floor = fm2.group(1).upper()
                        v = float(fm2.group(2))
                        area_val = round(v * 3.30579, 2) if fm2.group(3) == "坪" else v

                prop = Property(
                    site="居抜き市場",
                    property_id=prop_id,
                    name=card_text[:50],
                    address=address,
                    rent=rent,
                    area=area_val,
                    walk_minutes=walk,
                    nearest_station=station,
                    floor=floor,
                    url=url_full,
                )
                if _matches_filter(prop, cfg):
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
                if prop:
                    # ── 使用階を追加抽出 ──
                    # カードに "使用階 3" or "使用階/部屋番号 1/101A" と表示される
                    if not prop.floor:
                        card_text = card.get_text(" ", strip=True)
                        fm = re.search(r"使用階\s*/?\s*(?:部屋番号)?\s*(\d+)", card_text)
                        if fm:
                            prop.floor = fm.group(1) + "階"
                    if _matches_filter(prop, cfg, area_trusted=True):
                        results.append(prop)

            time.sleep(2)
    finally:
        page.context.close()
    logger.info(f"店舗ネットワーク: {len(results)} 件")
    return results


# ═══════════════════════════════════════════════════════
# メインエントリ
# ═══════════════════════════════════════════════════════
# アットホームは別ブラウザで実行（セッション汚染を避けるため）
SCRAPERS_MAIN = {
    "homes":        scrape_homes,
    "temposmart":   scrape_temposmart,
    "inshokuten":   scrape_inshokuten,
    "sonomama":     scrape_sonomama,
    "tenanto_biz":  scrape_tenanto_biz,
    "tenantshop":   scrape_tenantshop,
    "tempodas":     scrape_tempodas,
    "inuki_ichiba": scrape_inuki_ichiba,
    "temponw":      scrape_temponw,
}
SCRAPERS_STEALTH = {
    "athome": scrape_athome,   # 専用ブラウザ起動（CAPTCHA回避）
}


def _launch_browser(pw):
    return pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )


def run_scraper(config: dict) -> list[Property]:
    from playwright.sync_api import sync_playwright

    cfg_search = config["search"]
    sites_cfg = config.get("sites", {})
    all_props: list[Property] = []

    with sync_playwright() as pw:
        # ── 通常サイト（共有ブラウザ） ──
        browser = _launch_browser(pw)
        for site_key, scraper_fn in SCRAPERS_MAIN.items():
            if not sites_cfg.get(site_key, True):
                continue
            logger.info(f"── {site_key} 開始 ──")
            try:
                props = scraper_fn(cfg_search, browser)
                all_props.extend(props)
            except Exception as e:
                logger.error(f"{site_key} エラー: {e}")
        browser.close()

        # ── アットホーム（専用ブラウザ・セッション汚染なし） ──
        for site_key, scraper_fn in SCRAPERS_STEALTH.items():
            if not sites_cfg.get(site_key, True):
                continue
            logger.info(f"── {site_key} 開始 ──")
            browser2 = _launch_browser(pw)
            try:
                props = scraper_fn(cfg_search, browser2)
                all_props.extend(props)
            except Exception as e:
                logger.error(f"{site_key} エラー: {e}")
            finally:
                browser2.close()

    # 重複除去（サイト+ID）
    seen = set()
    unique = []
    for p in all_props:
        if p.unique_key not in seen:
            seen.add(p.unique_key)
            unique.append(p)

    logger.info(f"合計 {len(unique)} 件（重複除去後）")
    return unique
