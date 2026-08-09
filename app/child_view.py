"""子ども向けダッシュボードのデータ整形（docs/設計_子ダッシュボードと立て替え返済.md）。

**台帳（ledger）をそのまま子に見せてはいけない。**
`note` は運用者・開発者が書いたメモであり、子に見せる前提で書かれていない。
実データにはこういう値が入っている:

    「親攻撃テスト復元」
    「検証で誤って動かした分の復元(parent_grant上限テスト・N-11.17)」
    「restore_after_discord_id_amount_misparse」
    「gemini_assessed_total」

`extra` には Discord の実 ID も入っている。
そのまま返すと子の画面に出てしまうため、ここで**子向けに射影する**。

方針:
  ・`action` は**子向けの日本語ラベル**へ変換する（未知の action も安全側に倒す）
  ・`note` は**ホワイトリスト**。子自身の発話由来のものだけ通す
  ・`extra` は**返さない**
"""
import json
from collections import defaultdict
from pathlib import Path

# action → 子に見せるラベル。
# ここに無い action は「お金がうごいたよ」に倒す（内部用語を出さない）
_ACTION_LABELS = {
    "spending_record": "つかった",
    "allowance_grant": "おこづかい",
    "allowance_manual_grant": "おこづかい",
    "allowance_monthly_auto_grant": "おこづかい",
    "manual_income": "もらった",
    "goal_contribution": "ためた",
    "advance_repayment": "かえした",
    "balance_adjustment": "おうちの人がちょうせい",
    "wallet_check_correction": "おさいふチェックでちょうせい",
    "initial_setup": "さいしょのせってい",
}

# note を子へ見せてよい action。
# 子自身の発話が元になっているものだけ（品目・メモ）。
# 運用メモが入る action（balance_adjustment / data_recovery 等）は通さない
_NOTE_VISIBLE_ACTIONS = {
    "spending_record",   # 何を買ったか
    "manual_income",     # 何でもらったか
    "goal_contribution",  # 目標の名前
    "advance_repayment",  # 目標の名前
}

_FALLBACK_LABEL = "お金がうごいたよ"


def _load_ledger(log_dir: Path, name: str) -> list[dict]:
    """台帳を読む。壊れた行は飛ばす。

    Args:
        log_dir: ログの置き場所。
        name: 子の名前。

    Returns:
        list[dict]: 台帳の行。読めなければ空。
    """
    path = log_dir / f"{name}_wallet_ledger.jsonl"
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 壊れた行は無視する（画面を落とさない）
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def project_entry(row: dict) -> dict:
    """台帳の1行を、子に見せてよい形へ射影する。

    Args:
        row: 台帳の1行。

    Returns:
        dict: {"label", "amount", "note"}。**extra は含めない**。
    """
    action = str(row.get("action", ""))
    try:
        delta = int(row.get("delta", 0) or 0)
    except (TypeError, ValueError):
        delta = 0

    label = _ACTION_LABELS.get(action, _FALLBACK_LABEL)
    # note はホワイトリストの action だけ通す（運用メモを子に見せない）
    note = ""
    if action in _NOTE_VISIBLE_ACTIONS:
        note = str(row.get("note", "") or "").strip()
    return {"label": label, "amount": delta, "note": note}


def build_calendar(log_dir: Path, name: str, year: int, month: int) -> dict:
    """指定月のカレンダー用データを作る。

    日付の判定は **`ts[:10]` の文字列前方一致**で行う。
    台帳の ts は全件タイムゾーン付き ISO（+09:00）であることを実データで確認済み。
    `datetime.fromisoformat` でパースし直すと naive/aware 混在で TypeError を踏む余地があるため、
    既存ダッシュボード（server.py の月判定）と同じく文字列スライスで統一する。

    Args:
        log_dir: ログの置き場所。
        name: 子の名前。
        year: 対象の年。
        month: 対象の月。

    Returns:
        dict: {"year", "month", "days": {日: {"in": 入金合計, "out": 出金合計,
               "entries": [射影済みの明細]}}}
    """
    prefix = f"{int(year):04d}-{int(month):02d}"
    days: dict[int, dict] = defaultdict(lambda: {"in": 0, "out": 0, "entries": []})

    for row in _load_ledger(log_dir, name):
        ts = str(row.get("ts", ""))
        if not ts.startswith(prefix):
            continue
        try:
            day = int(ts[8:10])
        except ValueError:
            continue
        try:
            delta = int(row.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        cell = days[day]
        if delta >= 0:
            cell["in"] += delta
        else:
            cell["out"] += -delta
        cell["entries"].append(project_entry(row))

    return {"year": int(year), "month": int(month), "days": dict(days)}


def build_goals(goals: list[dict]) -> list[dict]:
    """目標（貯金・立て替え返済）を画面用に整える。

    進捗は **accumulated / target_amount** で出す。
    総残高で出すと、目標が2つあるとき同じお金が両方に計上され、
    残高が目標額を超えただけで達成扱いになる（2026/08/10 に是正）。

    Args:
        goals: wallet_state の savings_goals。

    Returns:
        list[dict]: 画面用の目標一覧。
    """
    result = []
    for g in goals if isinstance(goals, list) else []:
        if not isinstance(g, dict):
            continue
        # 取り消したものは子に見せない（2026/08/10）。
        # 親が桁を間違えて取り消した目標が「あと30,000円」として
        # 子の画面に残り続けると、返せない目標を見せ続けることになる
        if str(g.get("status", "active")) == "cancelled":
            continue
        try:
            target = int(g.get("target_amount", 0) or 0)
            accumulated = int(g.get("accumulated", 0) or 0)
        except (TypeError, ValueError):
            continue
        kind = str(g.get("kind", "saving"))
        pct = int(accumulated / target * 100) if target > 0 else 0
        result.append({
            "id": int(g.get("id", 0) or 0),
            "title": str(g.get("title", "")),
            "kind": kind,
            # 貯金は「ためた」、立て替えは「かえした」。同じバーで意味だけ変える
            "verb": "かえした" if kind == "advance" else "ためた",
            "target": target,
            "accumulated": accumulated,
            "remaining": max(target - accumulated, 0),
            "pct": min(max(pct, 0), 100),
            "status": str(g.get("status", "active")),
        })
    return result


def monthly_saved_history(log_dir: Path, name: str, months: int = 6) -> list[dict]:
    """月ごとの「ためた／かえした額」を返す（過去の自分との比較用）。

    社長承認済みの制約「貯金の可視化は**過去の自分と比較**」に使う。
    他の子の金額は一切扱わない（比較の対象は自分の過去だけ）。

    総残高ではなく **goal_contribution / advance_repayment の合計**で見る。
    残高は支出でも減るため、頑張ったかどうかと連動しないため。

    Args:
        log_dir: ログの置き場所。
        name: 子の名前。
        months: さかのぼる月数。

    Returns:
        list[dict]: [{"ym": "2026-08", "amount": 3000}, ...] 古い順。
    """
    totals: dict[str, int] = defaultdict(int)
    for row in _load_ledger(log_dir, name):
        action = str(row.get("action", ""))
        if action not in ("goal_contribution", "advance_repayment"):
            continue
        ym = str(row.get("ts", ""))[:7]
        if len(ym) != 7:
            continue
        try:
            totals[ym] += abs(int(row.get("delta", 0) or 0))
        except (TypeError, ValueError):
            continue

    ordered = sorted(totals.items())
    return [{"ym": ym, "amount": amount} for ym, amount in ordered[-int(months):]]
