"""
bot_utils.py — Bot共通ユーティリティ関数群

グローバル状態（Discord client・wallet_service 等）に依存しない
純粋関数のみをまとめる。bot.py の肥大化を防ぐために分離する。
"""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


def _thinking_message(age: int | None = None) -> str:
    """年齢に応じた「考え中」メッセージを返す。
    AI 呼び出し前に先送りして、応答待ちをユーザーに伝える。"""
    if age is not None and age <= 7:
        # 小学1〜2年生向け: ほぼひらがな
        return "かんがえてるよ…"
    if age is not None and age <= 9:
        # 小学3〜4年生向け: 混合
        return "かんがえ中だよ…"
    # 10歳以上・未設定
    return "考え中だよ…"


def _usage_guide_text() -> str:
    """子供向けの使い方案内テキストを返す（ベーステキスト）。
    年齢適応が必要な場合は bot.py 側で Gemini にリライトさせる。"""
    return (
        "【Compass Bot つかいかた】\n"
        "\n"
        "■ お小遣い相談\n"
        "`@compass-bot 〇〇を買いたい` など相談内容を送ると査定するよ\n"
        "\n"
        "■ ウォレット\n"
        "・残高確認: `残高おしえて`\n"
        "・残高報告: `残高 1234円`\n"
        "・初期設定: `初期設定` → 続けて `1234円`\n"
        "\n"
        "■ お金の記録\n"
        "・支出記録（メモのみ）: `支出記録` と送ると案内が出る\n"
        "・支出（残高から引く）: `支出 500円 お菓子`\n"
        "・入金（臨時収入）: `入金 3000円 お年玉`\n"
        "\n"
        "■ 貯金目標（最大5件）\n"
        "・目標設定: `貯金目標 ゲーム機 30000円`\n"
        "・進捗確認: `目標確認`\n"
        "・目標削除: `目標削除 ゲーム機` / `目標全削除`\n"
        "\n"
        "■ 振り返り・履歴\n"
        "・今月の振り返り: `振り返り`\n"
        "・査定履歴: `査定履歴`\n"
        "・入出金履歴: `入出金履歴` または `台帳確認`"
    )


def _usage_guide_text_parent() -> str:
    """親向けの使い方案内テキストを返す（親専用コマンドを網羅する）"""
    return (
        "【Compass Bot 親向けコマンド】\n"
        "\n"
        "■ お小遣い管理\n"
        "・手動支給: `支給 たろう 700円`\n"
        "・全員一括支給: `一括支給`\n"
        "・残高調整: `残高調整 たろう +500円` / `残高調整 たろう -300円`\n"
        "・固定額変更: `設定変更 たろう 固定 800円`\n"
        "・臨時上限変更: `設定変更 たろう 臨時 5000円`\n"
        "\n"
        "■ ダッシュボード・分析\n"
        "・全体確認: `全体確認`\n"
        "・傾向分析: `たろうの分析` / `全員の分析`\n"
        "\n"
        "■ 代理操作\n"
        "・代理投稿: `@compass-bot たろうの代理 内容`\n"
        "\n"
        "■ 管理・メンテナンス\n"
        "・全チャンネルに一斉アナウンス: `アナウンス [本文]`\n"
        "・残高チェック案内を即時送信: `@compass-bot 残高チェック送信`\n"
        "・使い方を全チャンネルに通知: `使い方の説明と初期設定`\n"
        "・リマインダーテスト: `@compass-bot reminder test`"
    )


def _rough_word_count(text: str) -> int:
    """テキストを句読点・空白で分割し、トークン数を返す"""
    tokens = [t for t in re.split(r"[\s、。,.!?！？]+", (text or "").strip()) if t]
    return len(tokens)


def _load_jsonl(path: Path) -> list[dict]:
    """JSONL ファイルを読み込み、dictのリストを返す。ファイルが存在しない場合は空リストを返す"""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # dictでないレコードは無視する
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                continue
    return rows


def _self_compare_message(log_dir: Path, user_name: str, current_score: int) -> str:
    """今回の満足度と過去平均を比較したメッセージを返す"""
    path = log_dir / f"{user_name}_pocket_journal.jsonl"
    rows = _load_jsonl(path)
    # 記録が1件以下の場合は比較不可
    if len(rows) <= 1:
        return "初回記録だよ。これから過去の自分との比較を作っていこう。"
    # 最新レコードを除いた過去の満足度の平均を計算する
    prev = [int(r.get("satisfaction", 0)) for r in rows[:-1] if isinstance(r.get("satisfaction"), int)]
    if not prev:
        return "比較データがまだ少ないので、記録を続けてみよう。"
    avg = sum(prev) / len(prev)
    diff = current_score - avg
    sign = "+" if diff >= 0 else ""
    return f"過去の自分平均は {avg:.1f}/10、今回は {current_score}/10（差分 {sign}{diff:.1f}）。"


def _recent_conversation_history(log_dir: Path, user_name: str, limit: int = 6) -> list[dict]:
    """直近の会話履歴を取得する。Geminiのコンテキスト継続に使用する"""
    path = log_dir / f"{user_name}_events.jsonl"
    rows = _load_jsonl(path)
    # 最新 limit 件を ts / user / assistant の形式に変換して返す
    picked: list[dict] = []
    for r in rows[-limit:]:
        picked.append(
            {
                "ts": r.get("ts"),
                "user": r.get("input"),
                "assistant": r.get("reply"),
            }
        )
    return picked


def _monthly_increase_stats(log_dir: Path, user_name: str, base_dt: datetime) -> dict:
    """当月・年間の査定金額増加回数と前回固定増額からの経過月を集計して返す"""
    path = log_dir / f"{user_name}_allowance_amounts.jsonl"
    rows = _load_jsonl(path)
    monthly_rows: list[dict] = []
    all_rows: list[dict] = []
    last_total: int | None = None
    last_fixed: int | None = None

    for r in rows:
        ts = r.get("ts")
        try:
            dt = datetime.fromisoformat(str(ts))
        except Exception:
            continue
        total = r.get("total")
        fixed = r.get("fixed")
        # total / fixed が整数でないレコードは集計対象外とする
        if not isinstance(total, int) or not isinstance(fixed, int):
            continue
        all_rows.append({"ts": dt, "total": total, "fixed": fixed})
        # base_dt と同じ月のレコードだけ月次集計に加える
        if dt.year == base_dt.year and dt.month == base_dt.month:
            monthly_rows.append({"ts": dt, "total": total, "fixed": fixed})
        last_total = total
        last_fixed = fixed

    # 月次での増加回数をカウントする（前レコードより増えていれば +1）
    monthly_rows.sort(key=lambda x: x["ts"])
    total_increase_count = 0
    fixed_increase_count = 0
    prev_total: int | None = None
    prev_fixed: int | None = None
    for r in monthly_rows:
        if prev_total is not None and r["total"] > prev_total:
            total_increase_count += 1
        if prev_fixed is not None and r["fixed"] > prev_fixed:
            fixed_increase_count += 1
        prev_total = r["total"]
        prev_fixed = r["fixed"]

    # 全期間から年間の固定増額回数と前回増額日を算出する
    all_rows.sort(key=lambda x: x["ts"])
    fixed_increase_count_this_year = 0
    last_fixed_increase_ts: datetime | None = None
    prev_fixed_all: int | None = None
    for r in all_rows:
        if prev_fixed_all is not None and r["fixed"] > prev_fixed_all:
            if r["ts"].year == base_dt.year:
                fixed_increase_count_this_year += 1
            last_fixed_increase_ts = r["ts"]
        prev_fixed_all = r["fixed"]

    # 前回固定増額から現在までの経過月を計算する
    months_since_last_fixed_increase: int | None = None
    if last_fixed_increase_ts is not None:
        months_since_last_fixed_increase = (
            (base_dt.year - last_fixed_increase_ts.year) * 12
            + (base_dt.month - last_fixed_increase_ts.month)
        )

    return {
        "total_increase_count": total_increase_count,
        "last_total": last_total,
        "last_fixed": last_fixed,
        "fixed_increase_count_month": fixed_increase_count,
        "fixed_increase_count_this_year": fixed_increase_count_this_year,
        "months_since_last_fixed_increase": months_since_last_fixed_increase,
    }


def _latest_assessed_amount(log_dir: Path, user_name: str) -> dict:
    """最後に確定した査定金額（fixed/temporary/total）を返す。未査定なら空dictを返す"""
    path = log_dir / f"{user_name}_allowance_amounts.jsonl"
    rows = _load_jsonl(path)
    # 末尾から走査して最初に全フィールドが揃っているレコードを採用する
    for r in reversed(rows):
        fixed = r.get("fixed")
        temporary = r.get("temporary")
        total = r.get("total")
        if isinstance(fixed, int) and isinstance(temporary, int) and isinstance(total, int):
            return {"fixed": fixed, "temporary": temporary, "total": total}
    return {}


def _normalize_assessed_amounts(
    user_conf: dict,
    assessed: dict | None,
    previous_assessed: dict | None,
) -> dict | None:
    """査定結果を正規化する。欠損フィールドは前回値またはユーザー設定で補完する"""
    if assessed is None:
        return None

    prev = previous_assessed or {}
    fixed = assessed.get("fixed")
    temporary = assessed.get("temporary")
    total = assessed.get("total")

    # fixed が未取得なら前回値、さらになければユーザー設定の固定額を使う
    if not isinstance(fixed, int):
        prev_fixed = prev.get("fixed")
        if isinstance(prev_fixed, int):
            fixed = prev_fixed
        else:
            fixed = int(user_conf.get("fixed_allowance", 0))

    # temporary が未取得なら前回値、さらになければ 0 とする
    if not isinstance(temporary, int):
        prev_tmp = prev.get("temporary")
        if isinstance(prev_tmp, int):
            temporary = prev_tmp
        else:
            temporary = 0

    # total が未取得なら fixed + temporary で算出する
    if not isinstance(total, int):
        total = int(fixed) + int(temporary)

    return {"fixed": int(fixed), "temporary": int(temporary), "total": int(total)}


def _extract_keyword_hits(user_conf: dict, input_text: str) -> dict:
    """ユーザー設定キーワードとの一致を investment / fun / danger の3バケツで返す"""
    text = (input_text or "").lower()
    kw = user_conf.get("keywords", {}) if isinstance(user_conf, dict) else {}
    if not isinstance(kw, dict):
        kw = {}

    def _hits(bucket: str) -> list[str]:
        # 各バケツのキーワードリストを取得し、テキストに含まれるものを抽出する
        items = kw.get(bucket, [])
        if not isinstance(items, list):
            return []
        matched: list[str] = []
        for x in items:
            token = str(x).strip()
            if token and token.lower() in text:
                matched.append(token)
        return matched

    return {
        "investment": _hits("investment"),
        "fun": _hits("fun"),
        "danger": _hits("danger"),
    }


def _normalize_keyword(text: str) -> str:
    """英数字以外を除去した小文字文字列を返す（キーワード一致判定用）"""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _contains_force_assess_keyword(input_text: str, keyword: str) -> bool:
    """強制査定テストキーワードが入力に含まれるか判定する"""
    if not keyword:
        return False
    # 両辺を正規化して部分一致で判定する
    return _normalize_keyword(keyword) in _normalize_keyword(input_text)


def _parse_fixed_delta_request(input_text: str) -> int | None:
    """「固定を+500円」「+500円」などの固定額変更リクエストを数値で返す。なければ None を返す"""
    text = (input_text or "").strip()
    # 「固定を±N円」の明示的な形式を優先する
    m = re.search(r"固定(?:を|は)?\s*([+-]\d+)\s*円", text)
    if m:
        return int(m.group(1))
    # 「±N円」の省略形にも対応する
    m2 = re.search(r"([+-]\d+)\s*円", text)
    if m2:
        return int(m2.group(1))
    return None


def _contains_any_keyword(text: str, keywords: list[str]) -> bool:
    """テキストにいずれかのキーワードが含まれるか判定する（大文字小文字を区別しない）"""
    body = (text or "").strip().lower()
    return any(k.lower() in body for k in keywords if k)


def _normalize_japanese_command(text: str) -> str:
    """日本語コマンド文字列を正規化する。空白・記号・句読点を除去して小文字にする"""
    body = (text or "").strip().lower()
    # 空白を除去する
    body = re.sub(r"\s+", "", body)
    # 記号・括弧・句読点を除去する
    body = re.sub(r"[、。,.!！?？:：`'\"「」『』\(\)\[\]【】]", "", body)
    return body


def _parse_yen_amount(text: str) -> int | None:
    """テキストから円金額を抽出して整数で返す。見つからなければ None を返す"""
    body = (text or "").strip()
    m = re.search(r"(\d[\d,]*)\s*円?", body)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _is_same_month(ts_str, year: int, month: int) -> bool:
    """タイムスタンプ文字列が指定した年月と一致するか判定する"""
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(str(ts_str))
        return dt.year == year and dt.month == month
    except Exception:
        return False


def _progress_bar(current: int, target: int, width: int = 10) -> str:
    """ASCII プログレスバーを返す。例: [████░░░░░░] 50%"""
    if target <= 0:
        return "[----------] 0%"
    # 達成率を 0.0〜1.0 にクランプして塗り潰し幅を算出する
    ratio = min(current / target, 1.0)
    filled = int(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(ratio * 100)
    return f"[{bar}] {pct}%"


def _spending_analysis_for_user(log_dir: Path, user_name: str, now_dt: datetime) -> str:
    """過去3ヶ月の支出傾向を文字列で返す"""
    path = log_dir / f"{user_name}_pocket_journal.jsonl"
    rows = _load_jsonl(path)

    # 過去3ヶ月の範囲を計算する（現在月を含む）
    months = []
    y, m = now_dt.year, now_dt.month
    for _ in range(3):
        months.append((y, m))
        # 1月を超えたら前年12月に折り返す
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()

    monthly_stats: list[str] = []
    all_items: list[str] = []
    for (yr, mo) in months:
        # 対象月のレコードだけを抽出する
        month_rows = [
            r for r in rows
            if _is_same_month(r.get("ts"), yr, mo)
        ]
        count = len(month_rows)
        # 満足度の平均を計算する（0件なら 0.0 とする）
        avg_sat = (
            sum(int(r.get("satisfaction", 0)) for r in month_rows) / count
            if count > 0 else 0
        )
        monthly_stats.append(f"{mo}月: {count}件 / 満足度{avg_sat:.1f}")
        all_items.extend(str(r.get("item", "")).strip() for r in month_rows if r.get("item"))

    # Counter で品目の出現頻度を集計して Top5 を取得する
    top5 = [item for item, _ in Counter(all_items).most_common(5)]
    top5_str = "・".join(top5) if top5 else "なし"

    lines = [f"【{user_name}の支出傾向（過去3ヶ月）】"]
    lines.extend(f"  {s}" for s in monthly_stats)
    lines.append(f"  よく使う品目: {top5_str}")
    return "\n".join(lines)


def _child_review_message(
    user_conf: dict,
    month_rows: list[dict],
    balance: int,
    year: int,
    month: int,
) -> str:
    """子供向けの今月の支出振り返りメッセージを年齢に応じて生成する。
    pocket_journal の当月レコードと現在残高を受け取り、まとめ文字列を返す。"""
    name = user_conf.get("name", "")
    raw_age = user_conf.get("age")
    # 年齢を int に正規化する（文字列数字も変換する）
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.strip().isdigit():
        age = int(raw_age.strip())
    else:
        age = None

    count = len(month_rows)
    # 記録ゼロ月は記録を促すメッセージを返す
    if count == 0:
        if age is not None and age <= 9:
            return (
                f"{name}さん、{month}月のきろくはまだないよ！\n"
                "なにかかったら「支出記録」でおしえてね。"
            )
        return (
            f"{name}さん、{month}月の支出記録はまだないよ！\n"
            "買ったものがあれば「支出記録」で記録してみてね。"
        )

    # 満足度の平均を算出する（satisfaction が None のレコードは除外する）
    sat_rows = [r for r in month_rows if r.get("satisfaction") is not None]
    avg_sat = sum(int(r["satisfaction"]) for r in sat_rows) / len(sat_rows) if sat_rows else None
    # 品目の出現頻度を集計して Top3 を取得する
    item_counts = Counter(str(r.get("item", "")).strip() for r in month_rows if r.get("item"))
    top3 = [item for item, _ in item_counts.most_common(3)]
    top3_str = "・".join(top3) if top3 else "なし"
    # amount フィールドがあるレコードを集計して総支出額を算出する
    total_amount = sum(int(r["amount"]) for r in month_rows if r.get("amount") is not None)
    # 金額集計行は合計額が1円以上のときのみ表示する
    amount_line = f"{total_amount:,}円" if total_amount > 0 else "記録なし"
    sat_line = f"{avg_sat:.1f}/10" if avg_sat is not None else "記録なし"

    if age is not None and age <= 9:
        # 低学年向け — ひらがな多め、シンプルな構成にする
        return (
            f"【{name}さんの{month}月のきろく】\n"
            f"かいたもの: {count}回\n"
            f"つかったがく: {amount_line}\n"
            f"まんぞくど: {sat_line}\n"
            f"かったもの: {top3_str}\n"
            f"いまのざんだか: {balance:,}円"
        )
    if age is not None and age <= 12:
        # 小学高学年向け — 数字と品目をわかりやすく並べる
        return (
            f"【{name}さんの{month}月の振り返り】\n"
            f"支出件数: {count}件\n"
            f"支出合計: {amount_line}\n"
            f"満足度平均: {sat_line}\n"
            f"使ったもの: {top3_str}\n"
            f"現在残高: {balance:,}円"
        )
    # 中学生以上 — 端的にまとめる
    return (
        f"【{name}さんの{month}月の支出まとめ】\n"
        f"件数: {count}件\n"
        f"合計支出: {amount_line}\n"
        f"満足度平均: {sat_line}\n"
        f"主な支出: {top3_str}\n"
        f"現在残高: {balance:,}円"
    )


def _assessment_history_message(user_conf: dict, rows: list[dict]) -> str:
    """直近の査定履歴を一覧形式でまとめて返す。
    rows は allowance_amounts.jsonl の直近N件（新しい順）を想定する。"""
    name = user_conf.get("name", "")
    raw_age = user_conf.get("age")
    # 年齢を int に正規化する
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.strip().isdigit():
        age = int(raw_age.strip())
    else:
        age = None

    # 記録がない場合は記録を促すメッセージを返す
    if not rows:
        if age is not None and age <= 9:
            return f"{name}さん、まだ査定のきろくはないよ！"
        return f"{name}さん、まだ査定の記録はないよ。"

    lines = []
    for r in rows:
        # タイムスタンプを「月/日」に整形する
        ts_str = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(str(ts_str))
            date_label = f"{dt.month}/{dt.day}"
        except Exception:
            date_label = "?"

        fixed = r.get("fixed")
        temporary = r.get("temporary")
        total = r.get("total")
        # None が入っている項目は「-」で表示して行を崩さない
        fixed_str = f"{fixed}円" if fixed is not None else "-"
        tmp_str = f"+{temporary}円" if temporary is not None else "-"
        total_str = f"{total}円" if total is not None else "-"
        lines.append(f"  {date_label}  固定{fixed_str} / 臨時{tmp_str} / 合計{total_str}")

    # 低学年のみひらがなヘッダーにする。それ以外は共通フォーマット
    if age is not None and age <= 9:
        header = f"【{name}さんのさていのきろく（さいきん{len(rows)}かい）】"
    else:
        header = f"【{name}さんの査定履歴（直近{len(rows)}件）】"

    return header + "\n" + "\n".join(lines)


def _ledger_history_message(user_conf: dict, rows: list[dict], limit: int = 10) -> str:
    """入出金台帳の直近N件を一覧形式でまとめて返す。
    rows は wallet_ledger.jsonl の全件を想定し、この関数内で末尾 limit 件に絞る。"""
    name = user_conf.get("name", "")
    raw_age = user_conf.get("age")
    # 年齢を int に正規化する
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.strip().isdigit():
        age = int(raw_age.strip())
    else:
        age = None

    # 末尾（最新）から limit 件を取り出して新しい順に並べる
    recent = list(reversed(rows[-limit:])) if rows else []

    # 記録がない場合は空の旨を伝える
    if not recent:
        if age is not None and age <= 9:
            return f"{name}さん、まだ入出金のきろくはないよ！"
        return f"{name}さん、まだ入出金の記録はないよ。"

    # action を日本語ラベルに変換するマッピング
    ACTION_LABELS = {
        "initial_setup":             "初期設定",
        "allowance_grant":           "査定支給",
        "allowance_manual_grant":    "手動支給",
        "allowance_monthly_auto_grant": "月次支給",
        "manual_expense":            "手動支出",
        "manual_income":             "臨時入金",
        "balance_adjustment":        "残高調整",
        "penalty":                   "ペナルティ",
    }

    lines = []
    for r in recent:
        # タイムスタンプを「月/日」に整形する
        ts_str = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(str(ts_str))
            date_label = f"{dt.month}/{dt.day}"
        except Exception:
            date_label = "?"

        action = str(r.get("action", ""))
        label = ACTION_LABELS.get(action, action)
        delta = int(r.get("delta", 0))
        after = int(r.get("balance_after", 0))
        # delta の符号を明示して方向が一目でわかるようにする
        delta_str = f"{delta:+d}円"
        lines.append(f"  {date_label}  {label}  {delta_str}  → {after}円")

    if age is not None and age <= 9:
        header = f"【{name}さんのお金のきろく（さいきん{len(recent)}けん）】"
    else:
        header = f"【{name}さんの入出金履歴（直近{len(recent)}件）】"

    return header + "\n" + "\n".join(lines)


def _build_goal_achieved_message(user_conf: dict, goal: dict) -> str:
    """目標達成時の祝福メッセージを年齢に応じて生成する。
    低学年ほどひらがな・感嘆符を多用し、達成感が伝わる文体にする。"""
    name = user_conf.get("name", "")
    title = goal.get("title", "目標")
    target = int(goal.get("target_amount", 0))

    # age を int に正規化する（未設定は None、文字列数字も変換する）
    raw_age = user_conf.get("age")
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.strip().isdigit():
        age = int(raw_age.strip())
    else:
        age = None

    if age is not None and age <= 9:
        # 低学年向け — ひらがな多め・感嘆符で達成感を強く伝える
        return (
            f"やったー！{name}さん、「{title}」の めざす きんがく {target:,}円 に とどいたよ！\n"
            "よくがんばったね！"
        )
    if age is not None and age <= 12:
        # 小学高学年向け — 達成の事実と称賛を簡潔に伝える
        return (
            f"{name}さん、目標「{title}」達成おめでとう！\n"
            f"目標額 {target:,}円 に到達したよ。よく続けたね！"
        )
    # 中学生以上 — 対等なトーンで端的に祝福する
    return (
        f"{name}さん、目標「{title}」達成！\n"
        f"目標額 {target:,}円 に到達したよ。お疲れさま。"
    )
