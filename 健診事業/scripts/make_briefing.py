#!/usr/bin/env python3
"""朝の司令書ジェネレーター（「今日なにやる？」の合図で実行）

リスト/ のCSVステータス・案件/ の進捗・正本/ の有無を読み、
司令書/{日付}.md に「今日やるべきこと」を1枚で出力する。

使い方:
    python3 make_briefing.py
"""

import csv
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 健診事業/
LIST_DIR = BASE_DIR / "リスト"
CASE_DIR = BASE_DIR / "案件"
SEIHON_DIR = BASE_DIR / "正本"
BRIEFING_DIR = BASE_DIR / "司令書"

SEIHON_FILES = ["商品設計書", "監査正本プロンプト", "営業文セット"]


def check_seihon():
    """正本3ファイルの有無を名前の部分一致で確認。"""
    existing = [p.name for p in SEIHON_DIR.glob("*.md") if not p.name.startswith("README")]
    missing = [key for key in SEIHON_FILES if not any(key in name for name in existing)]
    return existing, missing


def read_lists():
    """リストCSVを読み、ステータス別に集計。(csv名, 未着手, 下書き済, 送信済) のリスト。"""
    results = []
    for csv_path in sorted(LIST_DIR.glob("*.csv")):
        untouched, drafted, sent = [], [], []
        try:
            with csv_path.open(encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    status = (row.get("DM下書きステータス") or "").strip()
                    name = row.get("店名", "?")
                    if not status:
                        untouched.append(name)
                    elif "送信済" in status:
                        sent.append(f"{name}（{status}）")
                    else:
                        drafted.append(name)
        except Exception as e:
            print(f"警告: {csv_path.name} を読めませんでした: {e}")
            continue
        results.append({"name": csv_path.name, "未着手": untouched, "下書き済": drafted, "送信済": sent})
    return results


def read_cases():
    """案件フォルダの進捗を判定。"""
    cases = []
    if not CASE_DIR.exists():
        return cases
    for d in sorted(CASE_DIR.iterdir()):
        if not d.is_dir() or d.name == "_template":
            continue
        delivered = any(d.glob("納品済_*"))
        has = {n: (d / f).exists() and (d / f).stat().st_size > 0
               for n, f in [("hearing", "01_ヒアリング.md"),
                            ("audit", "02_一次監査結果.md"),
                            ("plan", "03_改善計画書_draft.md")]}
        if delivered:
            stage = "納品済み"
            next_action = "フォロー（必要なら）"
        elif has["plan"]:
            stage = "改善計画書ドラフトあり"
            next_action = "計画書のしょーた承認 → 04_納品/ へ確定版"
        elif has["audit"]:
            stage = "一次監査済み"
            next_action = "監査結果のしょーた承認 → 改善計画書ドラフト作成"
        elif has["hearing"]:
            stage = "ヒアリング済み"
            next_action = "一次監査の実施"
        else:
            stage = "着手前"
            next_action = "ヒアリングの実施・記録"
        cases.append({"name": d.name, "stage": stage, "next": next_action})
    return cases


def main():
    today = date.today().isoformat()
    _, seihon_missing = check_seihon()
    lists = read_lists()
    cases = read_cases()

    tasks = []       # (優先度番号, タスク, 所要目安)
    waiting = []     # 承認待ち・返信待ち
    questions = []   # しょーたに確認したいこと

    if seihon_missing:
        tasks.append((f"正本3ファイルのうち未設置の {len(seihon_missing)} 件"
                      f"（{'・'.join(seihon_missing)}）を 正本/ に置く", "15分"))
        questions.append("正本ファイルはいつ頃置けそうですか？（DM下書き・監査はこれ待ちです）")

    if not lists:
        tasks.append(("営業リストの初回生成（generate_list.py の実行）", "30分（APIキー設定込み）"))
    for lst in lists:
        if "_MOCK" in lst["name"]:
            tasks.append((f"モックCSV（{lst['name']}）を実データで作り直す（APIキー設定→実実行）", "30分"))
            continue
        if lst["未着手"] and not seihon_missing:
            top = "・".join(lst["未着手"][:3])
            tasks.append((f"DM下書き作成: {lst['name']} の上位から（次候補: {top}）", "1件15分"))
        if lst["下書き済"]:
            waiting.append(f"DM送信待ち（しょーた作業）: {'・'.join(lst['下書き済'])}")
        if lst["送信済"]:
            waiting.append(f"返信待ち: {'・'.join(lst['送信済'])}")

    for c in cases:
        tasks.append((f"案件「{c['name']}」（{c['stage']}）→ {c['next']}", "30〜60分"))
        if "承認" in c["next"]:
            waiting.append(f"しょーた承認待ち: 案件「{c['name']}」の{c['stage']}")

    if not tasks:
        tasks.append(("新しい業種×エリアでリスト生成、または既存リストの棚卸し", "30分"))

    lines = [
        f"# 司令書 {today}",
        "",
        f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}（make_briefing.py）",
        "",
        "## 今日やるべきこと（優先順）",
        "",
    ]
    for i, (task, mins) in enumerate(tasks, 1):
        lines.append(f"{i}. {task} 〔目安: {mins}〕")
    lines += ["", "## 承認待ち・返信待ち", ""]
    lines += [f"- {w}" for w in waiting] if waiting else ["- なし"]
    lines += ["", "## しょーたに確認したいこと", ""]
    lines += [f"- {q}" for q in questions] if questions else ["- なし"]
    lines.append("")

    BRIEFING_DIR.mkdir(parents=True, exist_ok=True)
    out = BRIEFING_DIR / f"{today}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"司令書を出力しました: {out}\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
