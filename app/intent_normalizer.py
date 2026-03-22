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
- spending_record: 支出記録フロー開始（「支出記録したい」「使ったものを記録したい」「ししゅつきろく」）
- manual_expense: 金額明示の即時支出記録（「支出 500円 お菓子」「500円つかった お菓子」）
- manual_income: 臨時収入の記録（「お年玉3000円もらった」「3000円入金した」）
- balance_report: 残高報告・照合（「いまのお金は1500円だよ」「残高は1500円です」）
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
- manual_expense は金額とアイテムが明示されている場合のみ（あいまいなら allowance_request か none）
- personality の値: "parent"（親っぽく）/ "sibling"（兄姉っぽく）/ "friend"（友達っぽく）/ "teacher"（先生っぽく）
- amount は整数（円）で返す。読み取れない場合は null
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


async def normalize_intent(text: str, gemini_service) -> dict:
    """ユーザーメッセージを AI 正規化して intent・entities・confidence を返す。
    戻り値例: {"intent": "balance_check", "entities": {...}, "confidence": "high"}
    失敗時は {"intent": "none", "entities": {}, "confidence": "high"} を返す（フォールバック）
    """
    # 空文字は分類不能として none を返す
    if not text or not text.strip():
        return {"intent": "none", "entities": {}, "confidence": "high"}

    prompt = _NORMALIZE_PROMPT.format(message=text.strip())
    try:
        # call_silent は「考え中...」を表示しない軽量呼び出し（コスト節約）
        raw = await gemini_service.call_silent(prompt)
        # Gemini の返答から JSON ブロックだけを正規表現で抽出する
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            result = json.loads(m.group())
            if isinstance(result, dict) and result.get("intent"):
                # entities が None の場合は空 dict に正規化する
                entities = result.get("entities") or {}
                return {
                    "intent": str(result["intent"]).strip(),
                    "entities": entities,
                    "confidence": str(result.get("confidence", "high")).strip(),
                }
    except Exception as e:
        # 正規化失敗はボット動作を止めないためログのみとする
        print(f"[intent_normalizer] error: {e}")

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
