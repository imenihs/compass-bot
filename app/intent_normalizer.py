"""intent_normalizer.py — B案 AI正規化レイヤー

Gemini 軽量モデルを使い、ユーザーメッセージの intent・entities・confidence を
JSON で返す。bot.py の on_message ディスパッチャーから呼び出す。
"""

import json
import re

# 分類プロンプトテンプレート — 19種の intent を定義する
_NORMALIZE_PROMPT = """\
あなたは子供のお小遣い管理ボット「Compass」のメッセージ分類器です。
以下のユーザーメッセージを分析し、JSONのみで返してください（説明文不要）。

【intent 一覧】
- balance_check: 残高・所持金の確認（「いくら持ってる？」「残高おしえて」「おこづかいいくら」）
- usage_guide: 使い方の説明（「つかいかたおしえて」「何ができるの？」「ヘルプ」）
- initial_setup: 初期設定の要求（「初期設定」「しょきせってい」「最初の設定したい」）
- spending_record: 支出記録（「支出記録したい」「お菓子を買った」「本かった」「何か買って使った」など。金額不明でもOK）
- manual_expense: 金額明示の即時支出記録（「支出 500円 お菓子」「500円つかった お菓子」— 金額とアイテムの両方が明示されている場合）
- manual_income: 臨時収入の記録（「お年玉3000円もらった」「3000円入金した」「もらった」「お金もらった」— 金額なしでもOK）
- balance_report: 財布の中身と帳簿の照合・残高報告（「いまのお金は1500円だよ」「残高は1500円です」「財布チェック」「財布の中身確認」— 金額なしでもOK）
- goal_check: 貯金目標の確認（「目標どのくらい？」「いくら貯まった？」「もくひょうかくにん」）
- goal_set: 貯金目標の設定（「ゲーム機のために30000円貯める」— タイトルと金額の両方が必要）
- goal_clear: 貯金目標の削除（「目標やめる」「もくひょうぜんさくじょ」「目標取り消して」）
- child_review: 今月・先月の振り返りレポート（「先月どうだった？」「振り返り」「こんげつのふりかえり」）
- assessment_history: 過去の査定確認（「過去の査定」「前回いくらだった？」「査定履歴」「さていれきし」）
- ledger_history: 入出金履歴の確認（「入出金履歴」「台帳見せて」「にゅうしゅつきんりれき」）
- dashboard: 全員の残高・状況確認（「みんなの状況は？」「全員の残高教えて」「ダッシュボード」）
- analysis_all: 全員の支出傾向分析（「みんなの使い方の傾向は？」「全体の傾向を分析して」）
- analysis_user: 特定ユーザーの支出傾向分析（「たろうの最近の支出は？」「〇〇の分析して」）
- personality_change: ボットの話し方変更（「友達みたいに話して」「先生っぽく話して」「お兄ちゃんモードで」）
- allowance_request: お小遣い増額・査定の相談（「お小遣い増やしてほしい」「本買いたいから増額して」）
- none: 雑談・上記のどれにも当てはまらない

【返却 JSON 形式】
{{"intent": "<intent名>", "entities": {{"target_name": null, "amount": null, "item": null, "reason": null, "satisfaction": null, "goal_title": null, "personality": null}}, "confidence": "high" または "low"}}

【判定ルール】
- intent が明確に判断できない場合は confidence を "low" にする
- goal_set は goal_title と amount の両方が読み取れる場合のみ（どちらか欠けたら none か goal_check）
- manual_expense は金額とアイテムの両方が明示されている場合のみ。金額がなければ spending_record を使う
- item はメッセージ中に買ったものが読み取れれば必ず抽出する（intent に関わらず）
- amount は整数（円）で返す。読み取れない場合は null
- personality の値: "parent"（親っぽく）/ "sibling"（兄姉っぽく）/ "friend"（友達っぽく）/ "teacher"（先生っぽく）
- target_name は分析・台帳対象のユーザー名。読み取れない場合は null
- 説明文不要。JSONのみ返す

ユーザーメッセージ: {message}
"""

# 各 intent に対応する確認メッセージ（confidence:low 時に表示する）
_INTENT_CONFIRM_QUESTIONS = {
    "balance_check": "残高を確認したいってこと？",
    "usage_guide": "使い方を知りたいってこと？",
    "initial_setup": "初期設定をしたいってこと？",
    "spending_record": "支出を記録したいってこと？",
    "manual_expense": "支出を記録するってこと？",
    "manual_income": "入金を記録するってこと？",
    "balance_report": "残高を報告したいってこと？",
    "goal_check": "貯金目標を確認したいってこと？",
    "goal_set": "貯金目標を設定したいってこと？",
    "goal_clear": "貯金目標を削除したいってこと？",
    "child_review": "今月の振り返りを見たいってこと？",
    "assessment_history": "過去の査定履歴を見たいってこと？",
    "ledger_history": "入出金履歴を見たいってこと？",
    "dashboard": "みんなのダッシュボードを見たいってこと？",
    "analysis_all": "全員の支出傾向を分析したいってこと？",
    "analysis_user": "誰かの支出傾向を分析したいってこと？",
    "personality_change": "話し方を変えたいってこと？",
    "allowance_request": "お小遣いの相談をしたいってこと？",
}
_ALLOWED_INTENTS = set(_INTENT_CONFIRM_QUESTIONS) | {"none"}
_ALLOWED_CONFIDENCE = {"high", "low"}

# 「はい」系の返答キーワード
_YES_KEYWORDS = {
    "そう", "はい", "うん", "yes", "ok", "おk", "そうだよ", "そうそう",
    "合ってる", "あってる", "そうです", "はい！", "うん！", "yeah", "yep",
    "oo", "おお", "そう！", "うん。", "はい。",
}

# 「いいえ」系の返答キーワード（スキップ・拒否・否定ワードを網羅する）
_NO_KEYWORDS = {
    "違う", "ちがう", "いや", "いいえ", "いいや", "no", "nope", "違います",
    "やめ", "やめる", "ちがいます", "別に", "そうじゃない", "ちがうよ",
    "なし", "ない", "いい", "いいです", "だいじょうぶ", "大丈夫",
    "スキップ", "skip", "パス", "pass",
}


def _entities(**overrides) -> dict:
    """intent 正規化の entities デフォルト値を作る"""
    base = {
        "target_name": None,
        "amount": None,
        "item": None,
        "reason": None,
        "satisfaction": None,
        "goal_title": None,
        "personality": None,
    }
    base.update(overrides)
    return base


def _amount_from_text(text: str) -> int | None:
    body = text or ""
    patterns: list[tuple[str, int]] = [
        (r"(\d[\d,]*)\s*万\s*(?:円|えん)", 10_000),
        (r"(\d[\d,]*)\s*(?:円|えん)", 1),
    ]
    for pattern, multiplier in patterns:
        m = re.search(pattern, body)
        if not m:
            continue
        try:
            amount = int(m.group(1).replace(",", "")) * multiplier
        except ValueError:
            return None
        return amount if amount > 0 else None
    return None


def _rough_expense_item(text: str) -> str | None:
    """支出っぽい短文から商品名だけを保守的に抜き出す"""
    body = re.sub(r"\d[\d,]*\s*万\s*(?:円|えん)(?:分)?", " ", text or "")
    body = re.sub(r"\d[\d,]*\s*(?:円|えん)(?:分)?", " ", body)
    body = re.sub(r"(自分で|支出|記録|買った|かった|使った|つかった|つかた|使いました|買いました)", " ", body)
    body = re.sub(r"[。！？!?,，、]", " ", body)
    item = " ".join(part for part in body.split() if part)
    return item or None


def _rough_goal_title(text: str) -> str | None:
    body = re.sub(r"\d[\d,]*\s*万\s*(?:円|えん)", " ", text or "")
    body = re.sub(r"\d[\d,]*\s*(?:円|えん)", " ", body)
    for marker in ["のために", "のため", "を貯めたい", "をためたい", "貯めたい", "ためたい", "貯金目標"]:
        if marker in body:
            body = body.split(marker)[0] if marker.startswith("のため") or marker.startswith("を") else body.replace(marker, " ")
            break
    body = re.sub(r"[。！？!?,，、]", " ", body)
    title = " ".join(part for part in body.split() if part)
    return title or None


def _rough_goal_clear_title(text: str) -> str | None:
    body = text or ""
    for marker in ["の目標やめ", "の目標削除", "の目標をやめ", "の目標を削除"]:
        if marker in body:
            title = body.split(marker, 1)[0]
            title = re.sub(r"[。！？!?,，、]", " ", title)
            return " ".join(part for part in title.split() if part) or None
    return None


def _rule_based_intent_fallback(text: str) -> dict | None:
    """Geminiが失敗/noneでも明確に処理できるお金系入力の保険"""
    body = (text or "").strip()
    if not body:
        return None
    compact = re.sub(r"\s+", "", body).lower()
    amount = _amount_from_text(body)

    if any(k in compact for k in ["使い方", "つかいかた", "ヘルプ", "なにができる"]):
        return {"intent": "usage_guide", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["どう記録", "記録方法", "やり方", "どうやって"]):
        return {"intent": "usage_guide", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["査定履歴", "さていれきし", "過去の査定", "前回いくら"]):
        return {"intent": "assessment_history", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["入出金履歴", "にゅうしゅつきんりれき", "台帳見せて", "台帳みせて"]):
        return {"intent": "ledger_history", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["振り返り", "ふりかえり", "今月どうだった", "先月どうだった"]):
        return {"intent": "child_review", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["初期設定", "しょきせってい", "最初の設定"]):
        return {"intent": "initial_setup", "entities": _entities(amount=amount), "confidence": "high"}

    if any(k in compact for k in ["みんなの状況", "全員の残高", "ダッシュボード", "全体確認", "ぜんたいかくにん"]):
        return {"intent": "dashboard", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["全体の傾向", "全員の分析", "みんなの使い方", "全体を分析"]):
        return {"intent": "analysis_all", "entities": _entities(), "confidence": "high"}

    m_analysis = re.search(r"(.+?)の分析", body)
    if m_analysis:
        return {"intent": "analysis_user", "entities": _entities(target_name=m_analysis.group(1).strip()), "confidence": "high"}

    if any(k in compact for k in ["友達みたい", "友だちみたい"]):
        return {"intent": "personality_change", "entities": _entities(personality="friend"), "confidence": "high"}
    if any(k in compact for k in ["先生っぽ", "先生みたい"]):
        return {"intent": "personality_change", "entities": _entities(personality="teacher"), "confidence": "high"}
    if any(k in compact for k in ["お兄ちゃん", "お姉ちゃん", "兄姉っぽ", "兄っぽ", "姉っぽ"]):
        return {"intent": "personality_change", "entities": _entities(personality="sibling"), "confidence": "high"}
    if any(k in compact for k in ["親っぽ", "お母さんみたい", "お父さんみたい"]):
        return {"intent": "personality_change", "entities": _entities(personality="parent"), "confidence": "high"}
    if "話して" in compact or "モード" in compact:
        return {"intent": "personality_change", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["お小遣い増やして", "おこづかい増やして", "増額して"]):
        return {"intent": "allowance_request", "entities": _entities(), "confidence": "high"}

    income_words = [
        "もらった", "もらいました", "もらた", "入金", "にゅうきん", "加算",
        "増えた", "ふえた", "お年玉", "おとしだま",
    ]
    if any(k in compact for k in income_words):
        item = None
        if "お年玉" in compact or "おとしだま" in compact:
            item = "お年玉"
        elif "お小遣い" in compact or "おこづかい" in compact:
            item = "お小遣い"
        return {"intent": "manual_income", "entities": _entities(amount=amount, item=item), "confidence": "high"}

    expense_words = ["買った", "かった", "使った", "つかった", "つかた", "支出"]
    if any(k in compact for k in expense_words):
        item = _rough_expense_item(body)
        intent = "manual_expense" if amount and item else "spending_record"
        return {
            "intent": intent,
            "entities": _entities(amount=amount, item=item),
            "confidence": "high",
        }

    if (
        "残高報告" in compact
        or "財布チェック" in compact
        or "さいふチェック" in compact
        or "ざいふチェック" in compact
        or "財布の中身" in compact
        or "さいふの中身" in compact
        or ("いまのお金" in compact and amount is not None)
        or ("今のお金" in compact and amount is not None)
    ):
        return {"intent": "balance_report", "entities": _entities(amount=amount), "confidence": "high"}

    balance_markers = ["残高", "ざんだか", "ざんがく", "所持金"]
    fuzzy_balance_markers = ["残方", "ざんかた", "残り", "のこり", "あといくら"]
    if (
        any(k in compact for k in balance_markers)
        or (
            any(k in compact for k in ["お金", "お小遣い", "おこづかい"])
            and any(k in compact for k in ["いくら", "教えて", "知りたい"] + fuzzy_balance_markers)
        )
    ):
        return {"intent": "balance_check", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["目標確認", "もくひょうかくにん", "もくひょかくにん", "目標どのくらい"]):
        return {"intent": "goal_check", "entities": _entities(), "confidence": "high"}

    if any(k in compact for k in ["目標やめ", "目標削除", "目標取り消", "もくひょうやめ", "もくひょうさくじょ"]):
        return {"intent": "goal_clear", "entities": _entities(goal_title=_rough_goal_clear_title(body)), "confidence": "high"}

    if any(k in compact for k in ["貯めたい", "貯金目標", "もくひょう", "もくひょ", "ためたい"]):
        return {
            "intent": "goal_set",
            "entities": _entities(amount=amount, goal_title=_rough_goal_title(body)),
            "confidence": "high",
        }

    return None


async def normalize_intent(text: str, gemini_service) -> dict:
    """ユーザーメッセージを AI 正規化して intent・entities・confidence を返す。
    戻り値例: {"intent": "balance_check", "entities": {...}, "confidence": "high"}
    失敗時は {"intent": "none", "entities": {}, "confidence": "high"} を返す（フォールバック）
    """
    # 空文字は分類不能として none を返す
    if not text or not text.strip():
        return {"intent": "none", "entities": {}, "confidence": "high"}

    fallback = _rule_based_intent_fallback(text)
    prompt = _NORMALIZE_PROMPT.format(message=text.strip())
    try:
        # call_silent は「考え中...」を表示しない軽量呼び出し（コスト節約）
        raw = await gemini_service.call_silent(prompt)
        # Gemini の返答から JSON ブロックだけを正規表現で抽出する
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            result = json.loads(m.group())
            if isinstance(result, dict) and result.get("intent"):
                intent = str(result["intent"]).strip()
                if intent not in _ALLOWED_INTENTS:
                    return fallback if fallback is not None else {"intent": "none", "entities": {}, "confidence": "high"}
                # entities が None の場合は空 dict に正規化する
                entities = result.get("entities") or {}
                if not isinstance(entities, dict):
                    return fallback if fallback is not None else {"intent": "none", "entities": {}, "confidence": "high"}
                confidence = str(result.get("confidence", "high")).strip()
                if confidence not in _ALLOWED_CONFIDENCE:
                    confidence = "low" if fallback is None else str(fallback.get("confidence", "high"))
                normalized = {
                    "intent": intent,
                    "entities": entities,
                    "confidence": confidence,
                }
                strict_fallback_intents = {"assessment_history", "ledger_history", "child_review"}
                if fallback is not None and fallback["intent"] in strict_fallback_intents and normalized["intent"] != fallback["intent"]:
                    return fallback
                if normalized["intent"] == "none" and fallback is not None:
                    return fallback
                if normalized["confidence"] == "low" and fallback is not None:
                    return fallback
                return normalized
    except Exception as e:
        # 正規化失敗はボット動作を止めないためログのみとする
        print(f"[intent_normalizer] error: {e}")

    if fallback is not None:
        return fallback

    # フォールバック: 査定フローに渡す
    return {"intent": "none", "entities": {}, "confidence": "high"}


def get_confirmation_question(intent: str) -> str:
    """intent に対応する確認質問文を返す（confidence:low 時に使用する）"""
    return _INTENT_CONFIRM_QUESTIONS.get(intent, "その操作のことってこと？")


def is_yes_reply(text: str) -> bool:
    """ユーザーの返答が「はい」系かどうかを判定する"""
    normalized = (text or "").strip().lower()
    return normalized in _YES_KEYWORDS


def is_no_reply(text: str) -> bool:
    """ユーザーの返答が「いいえ」系かどうかを判定する"""
    normalized = (text or "").strip().lower()
    return normalized in _NO_KEYWORDS
