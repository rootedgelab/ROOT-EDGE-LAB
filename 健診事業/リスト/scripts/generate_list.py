#!/usr/bin/env python3
"""営業候補リスト生成スクリプト（サイト健診事業）

Google Places API (New) の Text Search で「{業種} {エリア}」を検索し、
繁盛しているのにWebが弱い順に並べたCSVを リスト/ に出力する。

使い方:
    python3 generate_list.py --industry "整体・接骨院" --area "名古屋市中区"
    python3 generate_list.py --industry "整体・接骨院" --area "名古屋市中区" --mock  # APIキー不要の動作確認

ガードレール:
- データ取得は公式 Places API のみ（スクレイピング禁止）
- 1回の実行上限は60件。APIリクエスト数と概算コストを表示する
- 通過店舗のサイトチェックはトップページ1回のみ・間隔1秒以上
- APIキーは .env の GOOGLE_PLACES_API_KEY から読む（直書き禁止）
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

SCRIPT_DIR = Path(__file__).resolve().parent
LIST_DIR = SCRIPT_DIR.parent          # 健診事業/リスト/
BASE_DIR = LIST_DIR.parent            # 健診事業/
LOG_DIR = BASE_DIR / "logs"
MOCK_FILE = SCRIPT_DIR / "mock_data" / "places_mock.json"

# --- Places API (New) ---
PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.rating",
    "places.userRatingCount",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.googleMapsUri",
    "nextPageToken",
])
PAGE_SIZE = 20
MAX_RESULTS = 60  # 実行上限（変更しないこと）
# rating / websiteUri 等を含むため Text Search (Enterprise) SKU。1リクエスト約$0.035
COST_PER_REQUEST_USD = 0.035

# --- 一次フィルタ ---
RATING_MIN = 4.3
REVIEWS_MIN = 50

# --- サイト機械チェック ---
SITE_TIMEOUT = 10          # 秒
SITE_INTERVAL = 1.0        # サイト間の待機（秒）。負荷をかけない
SLOW_THRESHOLD = 3.0       # これより遅ければ「応答が遅い」
COPYRIGHT_STALE_YEARS = 3  # コピーライト年がこれ以上前なら「更新が古い」
USER_AGENT = "Mozilla/5.0 (compatible; site-kenshin-precheck/1.0)"

# --- サイト弱さスコアの配点 ---
WEAK_POINTS = {
    "https": ("HTTPS非対応（httpのまま）", 30),
    "unreachable": ("サイトにアクセスできない/エラー", 25),
    "viewport": ("viewportメタタグなし（スマホ非対応の疑い）", 25),
    "copyright": ("コピーライト年が古い", 15),
    "title": ("タイトルタグなし", 10),
    "description": ("ディスクリプションなし", 10),
    "slow": ("応答が遅い", 10),
}


def search_places(api_key: str, query: str):
    """Text Search を最大60件までページングして取得。(places, request_count) を返す。"""
    places = []
    page_token = None
    request_count = 0
    while len(places) < MAX_RESULTS:
        body = {"textQuery": query, "languageCode": "ja", "pageSize": PAGE_SIZE}
        if page_token:
            body["pageToken"] = page_token
        resp = requests.post(
            PLACES_ENDPOINT,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            timeout=30,
        )
        request_count += 1
        if resp.status_code != 200:
            sys.exit(f"Places APIエラー (HTTP {resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        places.extend(data.get("places", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(1)
    return places[:MAX_RESULTS], request_count


def load_mock_places():
    with MOCK_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return data["places"], 0


def parse_place(p: dict) -> dict:
    return {
        "店名": p.get("displayName", {}).get("text", ""),
        "評価": p.get("rating"),
        "口コミ数": p.get("userRatingCount", 0),
        "住所": p.get("formattedAddress", ""),
        "電話": p.get("nationalPhoneNumber", ""),
        "サイトURL": p.get("websiteUri", ""),
        "Googleマップ": p.get("googleMapsUri", ""),
        "_mock_site": p.get("_mock_site"),  # モックデータ用
    }


def passes_filter(shop: dict) -> bool:
    return (
        (shop["評価"] or 0) >= RATING_MIN
        and shop["口コミ数"] >= REVIEWS_MIN
        and bool(shop["サイトURL"])
    )


def analyze_html(url: str, final_url: str, html: str, elapsed: float) -> list:
    """静的解析で弱点キーのリストを返す。"""
    weak = []
    if final_url.lower().startswith("http://") or url.lower().startswith("http://"):
        weak.append("https")
    if not re.search(r"<meta[^>]+name=[\"']viewport", html, re.I):
        weak.append("viewport")
    if not re.search(r"<title[^>]*>\s*[^<\s]", html, re.I):
        weak.append("title")
    if not re.search(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'][^\"']+", html, re.I) and \
       not re.search(r"<meta[^>]+content=[\"'][^\"']+[\"'][^>]+name=[\"']description[\"']", html, re.I):
        weak.append("description")
    years = re.findall(r"(?:©|&copy;|&#169;|copyright)\D{0,30}?((?:19|20)\d{2})", html, re.I)
    if years and max(int(y) for y in years) <= date.today().year - COPYRIGHT_STALE_YEARS:
        weak.append("copyright")
    if elapsed > SLOW_THRESHOLD:
        weak.append("slow")
    return weak


def check_site(shop: dict, mock: bool) -> tuple:
    """トップページ1回のみの機械チェック。(弱点キーのリスト, 応答秒) を返す。"""
    if mock:
        m = shop.get("_mock_site") or {}
        return list(m.get("weak_keys", [])), m.get("elapsed", 0.0)

    url = shop["サイトURL"]
    try:
        start = time.monotonic()
        resp = requests.get(
            url, timeout=SITE_TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True
        )
        elapsed = time.monotonic() - start
        if resp.status_code >= 400:
            return ["unreachable"], elapsed
        return analyze_html(url, str(resp.url), resp.text, elapsed), elapsed
    except requests.RequestException:
        return ["unreachable"], SITE_TIMEOUT


def weak_score(weak_keys: list) -> int:
    return sum(WEAK_POINTS[k][1] for k in weak_keys)


def weak_labels(weak_keys: list, elapsed: float) -> str:
    labels = []
    for k in weak_keys:
        label = WEAK_POINTS[k][0]
        if k == "slow":
            label += f"（{elapsed:.1f}秒）"
        labels.append("・" + label)
    return "\n".join(labels) if labels else "（機械チェックでは大きな弱点なし）"


def main():
    parser = argparse.ArgumentParser(description="営業候補リスト生成（サイト健診事業）")
    parser.add_argument("--industry", required=True, help="業種（例: 整体・接骨院）")
    parser.add_argument("--area", required=True, help="エリア（例: 名古屋市中区）")
    parser.add_argument("--mock", action="store_true", help="APIキー不要のモックデータで動作確認")
    args = parser.parse_args()

    query = f"{args.industry} {args.area}"
    print(f"検索クエリ: {query}" + ("（モックモード）" if args.mock else ""))

    if args.mock:
        raw_places, request_count = load_mock_places()
    else:
        load_dotenv(SCRIPT_DIR / ".env")
        api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
        if not api_key or "ここに" in api_key:
            sys.exit(
                "エラー: APIキーが見つかりません。\n"
                f"  {SCRIPT_DIR / '.env.example'} を .env にコピーし、\n"
                "  GOOGLE_PLACES_API_KEY に実際のキーを記入してください。"
            )
        raw_places, request_count = search_places(api_key, query)

    shops = [parse_place(p) for p in raw_places]
    print(f"取得件数: {len(shops)} 件（上限 {MAX_RESULTS} 件）")
    print(f"APIリクエスト数: {request_count} 回 / 概算コスト: ${request_count * COST_PER_REQUEST_USD:.3f}")
    print("  ※ Text Search (Enterprise SKU, 約$0.035/回) 換算。無料枠の範囲は Google Cloud コンソールで確認のこと")

    passed = [s for s in shops if passes_filter(s)]
    print(f"一次フィルタ通過（評価{RATING_MIN}以上・口コミ{REVIEWS_MIN}件以上・サイトあり）: {len(passed)} 件")

    for i, shop in enumerate(passed):
        print(f"  サイトチェック {i + 1}/{len(passed)}: {shop['店名']}")
        keys, elapsed = check_site(shop, args.mock)
        shop["_weak_keys"] = keys
        shop["_elapsed"] = elapsed
        shop["サイト弱さスコア"] = weak_score(keys)
        shop["繁盛度スコア"] = round((shop["評価"] or 0) * shop["口コミ数"], 1)
        if not args.mock and i < len(passed) - 1:
            time.sleep(SITE_INTERVAL)  # 負荷をかけない

    # 「商売は強いのにWebが弱い」順: 両スコアを正規化した積で並べる
    max_hanjo = max((s["繁盛度スコア"] for s in passed), default=1) or 1
    max_weak = max((s["サイト弱さスコア"] for s in passed), default=1) or 1
    for s in passed:
        s["_priority_score"] = (s["繁盛度スコア"] / max_hanjo) * (s["サイト弱さスコア"] / max_weak)
    passed.sort(key=lambda s: (-s["_priority_score"], -s["繁盛度スコア"]))

    today = date.today().isoformat()
    suffix = "_MOCK" if args.mock else ""
    out_path = LIST_DIR / f"{today}_{args.industry}_{args.area}{suffix}.csv"
    columns = [
        "優先順位", "店名", "評価", "口コミ数", "サイトURL", "検出された弱点",
        "DM下書きステータス", "繁盛度スコア", "サイト弱さスコア", "住所", "電話", "Googleマップ",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for rank, s in enumerate(passed, 1):
            writer.writerow({
                "優先順位": rank,
                "店名": s["店名"],
                "評価": s["評価"],
                "口コミ数": s["口コミ数"],
                "サイトURL": s["サイトURL"],
                "検出された弱点": weak_labels(s["_weak_keys"], s["_elapsed"]),
                "DM下書きステータス": "",
                "繁盛度スコア": s["繁盛度スコア"],
                "サイト弱さスコア": s["サイト弱さスコア"],
                "住所": s["住所"],
                "電話": s["電話"],
                "Googleマップ": s["Googleマップ"],
            })

    # 実行ログ
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{today}_リスト生成.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] query={query} mock={args.mock} "
            f"取得={len(shops)} 通過={len(passed)} APIリクエスト={request_count} "
            f"概算=${request_count * COST_PER_REQUEST_USD:.3f} 出力={out_path.name}\n"
        )

    print(f"\n出力: {out_path}")
    print(f"実行ログ: {log_path}")


if __name__ == "__main__":
    main()
