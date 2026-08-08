"""子の心身の安全に関わる危険信号の検知と通知（N-11.16）。

このモジュールの役割は「LLM が下げられない床」を Python 側に置くことである。
お金の安全（二重支給・越境）は tool 内 Python で必ず検証し AI の判断に委ねない設計になっている。
より重い子の心身の安全を AI 任せにするのは一貫性を欠くため、決定論的な検知をここに持つ。
LLM はこの床の上乗せ（微妙なケースの拾い上げ・会話での寄り添い）を担当する。

最重要の設計判断は「虐待の示唆を親チャンネルへ通知しない」ことである。
児童虐待の加害者は同居の実親であることが多く、親へ流すと加害者への情報還流となり、
口封じ・報復・エスカレーションを招く。子には親を介さず単独で到達できる公的窓口を渡す。
"""
import re
import unicodedata

# 公的な相談窓口。LLM に電話番号を生成させると誤った番号を子に渡す危険があるため定数で持つ
# （自殺予防の文脈で番号のハルシネーションは最悪の失敗）。番号は変更時にここだけ直す。
HOTLINES = {
    # 虐待・親に言えないことを、親を介さず相談できる窓口
    "abuse": [
        "児童相談所 虐待対応ダイヤル 189（いちはやく・24時間・無料）",
        "チャイルドライン 0120-99-7777（18歳まで・毎日16時〜21時・無料）",
        "24時間子供SOSダイヤル 0120-0-78310（24時間・無料）",
    ],
    # 死にたい気持ち・つらさを相談できる窓口
    "self_harm": [
        "チャイルドライン 0120-99-7777（18歳まで・毎日16時〜21時・無料）",
        "よりそいホットライン 0120-279-338（24時間・無料）",
        "24時間子供SOSダイヤル 0120-0-78310（24時間・無料）",
    ],
    # いじめを相談できる窓口
    "bullying": [
        "24時間子供SOSダイヤル 0120-0-78310（24時間・無料）",
        "チャイルドライン 0120-99-7777（18歳まで・毎日16時〜21時・無料）",
    ],
}

# 緊急時の通報先。切迫している場合に必ず添える
EMERGENCY_LINE = "いのちが今あぶないと思ったら、119番（救急）や110番（警察）にかけていいよ。"

# --- 検知パターン ---------------------------------------------------------
# 床であるため、確実に危険と分かる語だけを置く。曖昧な語で過検知すると
# 親が麻痺して本当の信号を見落とす（狼少年化）。微妙なケースは LLM 側が拾う。

# 切迫した希死念慮・自傷。最優先で拾う
_URGENT_SELF_HARM = [
    r"死にたい", r"しにたい", r"消えたい", r"きえたい",
    r"自殺", r"じさつ", r"生きてる意味", r"いきてる意味",
    r"リストカット", r"リスカ", r"手首を切", r"死のうと",
    r"居なくなりたい", r"いなくなりたい",
]

# 虐待・ネグレクトの示唆。親へ通知してはならないカテゴリ。
# 家庭内の加害者を表す語を広く列挙し、暴力語と組み合わせて検出する。
# 「お父さんに殴られた」を _BULLYING の「殴られ」に先取りされると虐待が親へ通知されるため、
# 加害者語 × 暴力語の組み合わせは必ずこちらで拾う（実機テストで再現した事故）。
_FAMILY = r"(親|おや|父|ちち|お父さん|おとうさん|パパ|とうちゃん|母|はは|お母さん|おかあさん|ママ|かあちゃん|義父|義母|継父|継母|兄|姉|じいちゃん|ばあちゃん|祖父|祖母|家族|うち)"
_VIOLENCE = r"(殴|なぐ|叩|たた|蹴|け|ぶた|暴力|どなら|怒鳴ら|投げつけ|閉じ込め|首をしめ|しめられ)"
_ABUSE = [
    # 家族 × 暴力（受け身・被害の形）。例「お父さんに殴られた」「ママにたたかれる」
    rf"{_FAMILY}(に|から)[^。！？]{{0,6}}{_VIOLENCE}",
    # ネグレクト
    r"家に帰りたくない", r"うちに帰りたくない", r"帰るのがこわい", r"帰るのが怖い",
    r"ごはんがもらえ", r"ごはんが無い", r"ごはんがない", r"ごはんぬき", r"ごはん抜き",
    r"ごはんを食べさせてもらえ", r"何日も食べて", r"お風呂に入らせてもらえ",
    # 明示語
    r"体罰", r"虐待", r"ネグレクト",
    # 性的虐待の示唆
    r"裸の写真を撮られ", r"体をさわられ", r"からだをさわられ", r"さわられるのがいや",
]

# いじめ・暴力被害
_BULLYING = [
    r"いじめられ", r"イジメられ", r"無視される", r"むしされる",
    r"殴られ", r"なぐられ", r"たたかれる", r"蹴られ", r"けられる",
    r"仲間はずれ", r"なかまはずれ", r"晒され", r"さらされ",
    r"カツアゲ", r"かつあげ", r"お金を取られ", r"おかねをとられ",
    r"パシリ", r"死ねって言われ", r"しねって言われ",
]

# 性的搾取・グルーミング。お金の文脈に現れやすくこの BOT が拾うべき類型
_GROOMING = [
    r"写真を送って", r"写真送って", r"自撮りを送", r"裸の写真",
    r"ネットで知り合った", r"会おうって言われ", r"会おうと言われ",
    r"パパ活", r"援助交際", r"えんこう",
    r"お金あげるから", r"おこづかいあげるから",
    r"内緒で会", r"ないしょで会",
]

# 闇バイト・違法な稼ぎ方。金銭文脈に固有
_ILLEGAL_WORK = [
    r"簡単に稼げる", r"かんたんに稼げる", r"высок",  # 保険
    r"闇バイト", r"やみバイト", r"高額バイト",
    r"受け子", r"出し子", r"叩き",
]

# 喫煙・飲酒・薬物。文脈が明確なときのみ通知する（特異度優先）
_SUBSTANCE = [
    r"たばこ.*(買|吸)", r"タバコ.*(買|吸)", r"煙草.*(買|吸)",
    r"お酒.*(買|飲)", r"ビール.*(買|飲)", r"酒を.*(買|飲)",
    r"大麻", r"覚醒剤", r"オーバードーズ", r"市販薬.*(大量|たくさん)",
]

# 摂食障害の兆候。ダイエット関連の購入としてこの BOT に現れる
_EATING = [
    r"food",  # 保険
    r"吐いた", r"はいた.*ダイエット", r"食べたのを吐",
    r"下剤", r"げざい", r"痩せ薬", r"やせ薬",
    r"食べるのが怖", r"太るのが怖",
]

# カテゴリ定義。key -> (パターン群, 切迫度, 親へ通知するか, 窓口キー)
# 切迫度: urgent=子の拒否を押し切って即通知 / high=通知するが伝え方を子と決める / medium=文脈が明確なときのみ
_CATEGORIES = [
    # 虐待は親へ通知しない（notify_parent=False）。これが本モジュール最重要の分岐
    ("abuse", _ABUSE, "urgent", False, "abuse"),
    ("self_harm", _URGENT_SELF_HARM, "urgent", True, "self_harm"),
    ("grooming", _GROOMING, "urgent", True, "abuse"),
    ("bullying", _BULLYING, "high", True, "bullying"),
    ("illegal_work", _ILLEGAL_WORK, "high", True, None),
    ("eating", _EATING, "high", True, "self_harm"),
    ("substance", _SUBSTANCE, "medium", True, None),
]

_COMPILED = [
    (name, [re.compile(p) for p in pats], urgency, notify_parent, hotline)
    for name, pats, urgency, notify_parent, hotline in _CATEGORIES
]


def _normalize(text: str) -> str:
    """検知用に正規化する。全角半角・記号のゆれで見逃さないようにする。

    NFKC で全角半角を統一し、空白と一部の区切り記号を除く。
    「死 に た い」のような分かち書きでの回避を防ぐ意図もある。
    """
    t = unicodedata.normalize("NFKC", text or "")
    # 空白類を除去する。分かち書きによる回避を防ぐ
    return re.sub(r"[\s　]+", "", t)


def detect(text: str) -> dict | None:
    """発話に危険信号が含まれるか判定する（LLM が下げられない決定論的な床）。

    複数カテゴリに当たった場合は、切迫度が高いものを優先する。
    虐待が他カテゴリと同時に当たった場合も虐待を優先し、親への通知を止める側に倒す
    （安全側の既定。加害者へ情報が渡るのを防ぐことを最優先する）。

    Args:
        text: 子の発話。

    Returns:
        dict | None: 検知したら
            {"category": str, "urgency": "urgent"|"high"|"medium",
             "notify_parent": bool, "hotline_key": str|None, "matched": str}
            検知しなければ None。
    """
    if not text:
        return None
    norm = _normalize(text)
    order = {"urgent": 0, "high": 1, "medium": 2}
    best = None
    for name, pats, urgency, notify_parent, hotline in _COMPILED:
        for p in pats:
            m = p.search(norm)
            if not m:
                continue
            cand = {
                "category": name, "urgency": urgency,
                "notify_parent": notify_parent, "hotline_key": hotline,
                "matched": m.group(0),
            }
            # 虐待は無条件で最優先（親へ流さない判断を他カテゴリに上書きさせない）
            if name == "abuse":
                return cand
            if best is None or order[urgency] < order[best["urgency"]]:
                best = cand
            break
    return best


def build_ai_judge_prompt(text: str, age: int | None) -> str:
    """AI に危険信号を意味で判定させるための指示文を組み立てる（判定の主）。

    Python の検知は完全一致しか見えず、方言・言い換え・遠回しな表現で素通りする
    （例「おとんにどつかれた」「うちの人に手をあげられる」「パパがこわい。夜になると」）。
    表現のゆれに強い意味判断は AI が担い、Python は絶対に落としてはいけない語だけを保証する。

    Args:
        text: 子の発話。
        age: 子の年齢。同じ言葉でも年齢で意味が変わるため判断材料として渡す。

    Returns:
        str: claude へ渡す判定用プロンプト。JSON で返させる。
    """
    age_note = ""
    if isinstance(age, int):
        age_note = (
            f"\nこの子は{age}歳。年齢で意味が変わることに注意する。"
            "低年齢の『死にたい』は language の借用のことが多い一方、"
            "低年齢で年齢に不相応な性的知識が出るのは被害のサインである可能性が高い。"
        )
    return (
        "あなたは子どもの安全を見守る専門家だ。次の子どもの発話に、危険信号が含まれるか判定する。\n"
        f"発話: 「{text}」{age_note}\n\n"
        "【判定するカテゴリ】\n"
        "- abuse: 家庭内の虐待・ネグレクト（家族からの暴力/暴言、食事や入浴をさせてもらえない、"
        "家に帰りたくない、家族から性的な接触）。**加害者が家族であることが要点**。\n"
        "- self_harm: 自傷・希死念慮（死にたい、消えたい、自分を傷つけている）。\n"
        "- grooming: 性的搾取・グルーミング（見知らぬ人から写真の要求、会おうと誘われる、"
        "お金と引き換えの誘い、パパ活・援助交際）。\n"
        "- bullying: いじめ・暴力被害（**家族以外**からの暴力・無視・晒し・恐喝）。\n"
        "- illegal_work: 闇バイト・違法な稼ぎ方。\n"
        "- eating: 摂食障害の兆候（嘔吐、下剤・痩せ薬、食べるのが怖い）。\n"
        "- substance: 喫煙・飲酒・薬物。\n"
        "- none: 危険信号なし。\n\n"
        "【最重要の区別】\n"
        "加害者が**家族**なら abuse、**家族以外**なら bullying。ここを取り違えてはいけない。\n"
        "方言・遠回しな言い方・主語の省略に注意する。"
        "例『おとんにどつかれた』『うちの人に手をあげられる』『パパがこわい、夜になると』は abuse。\n"
        "誰にやられたか分からない場合は、判断を急がず uncertain を true にする。\n\n"
        "【切迫度 urgency】\n"
        "- urgent: 今まさに危ない（今夜死ぬつもり・手段を用意している・現在進行の暴力）\n"
        "- high  : 放置できない（死にたいと思うことがある・継続しているいじめ）\n"
        "- medium: 気になる（一度きりの発言・軽微な逸脱）\n"
        "- none  : 危険信号なし\n\n"
        "【判定の確信度 confidence】0.0〜1.0 の数値で返す。\n"
        "- 0.9以上: 発話だけで明確に判断できる\n"
        "- 0.5〜0.9: そう解釈するのが自然だが、別の意味の可能性も残る\n"
        "- 0.5未満: 判断材料が足りない・言葉の綾かもしれない\n"
        "言葉の綾や比喩（例『このコード殺したい』『バグで死ぬほど疲れた』『機能を消したい』）は\n"
        "category を none にし、confidence を高くする（危険でないと確信できるため）。\n\n"
        "【加害者が誰か perpetrator】family（家族）/ outside（家族以外）/ unknown（分からない）\n"
        "暴力・被害の訴えでは必ず判断する。分からなければ unknown にし、決めつけない。\n\n"
        "JSON だけを返す。説明を書かない。\n"
        '{"category":"...","urgency":"...","confidence":0.0,'
        '"perpetrator":"family|outside|unknown","uncertain":true/false,"reason":"日本語で一文"}'
    )


def merge_judgments(py_result: dict | None, ai_result: dict | None) -> dict | None:
    """Python の床と AI の意味判断を統合する（二重化の要）。

    方針は3つ。
    ① **どちらかが危険と言えば危険**（OR 結合）。片方が見逃してももう片方が拾う。
    ② **親へ通知するかは安全側に倒す**。どちらかが「虐待の可能性」と言えば親へ送らない。
       Python が bullying と判定していても、AI が abuse と言えば親通知を止める。
       加害者へ情報が渡る事故は取り返しがつかないため、迷ったら送らない側に倒す。
    ③ **AI が「誰にやられたか不明」と言った場合も親へ送らない**。
       家族が加害者である可能性を否定できない以上、送るリスクを取らない。

    Args:
        py_result: detect() の結果。
        ai_result: AI 判定の JSON をパースした dict（category/urgency/uncertain）。

    Returns:
        dict | None: 統合結果。どちらも検知なしなら None。
    """
    ai = ai_result or {}
    ai_cat = ai.get("category")
    ai_said_none = ai_cat == "none"
    if ai_said_none:
        ai_cat = None
    py_cat = (py_result or {}).get("category")
    if not ai_cat and not py_cat:
        return None

    # AI の確信度。数値でなければ「判断材料が足りない」とみなし低く扱う（安全側）
    try:
        confidence = float(ai.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    # AI が見た加害者。family なら虐待として扱い親へ送らない
    perpetrator = ai.get("perpetrator") if ai.get("perpetrator") in ("family", "outside", "unknown") else "unknown"

    # AI が明示的に「危険でない」と判断した場合は、通知せず記録だけに落とす。
    # Python 床は意図的に粗く言葉の綾を拾う（実測「この機能は消えたい…じゃなくて消したい」を
    # self_harm と誤検知）。毎回親へ送ると親が麻痺し本当の信号を見落とす（狼少年化）。
    # ただし確信度が低い否定（0.7未満）は AI 自身が迷っているので、抑制せず通常判定へ回す。
    # 虐待は AI が否定しても抑制しない（見逃しの損失が回復不能なため必ず記録を残す）。
    if ai_said_none and py_cat and py_cat != "abuse" and confidence >= 0.7:
        return {
            "category": py_cat,
            "urgency": (py_result or {}).get("urgency", "medium"),
            "notify_parent": False,      # 送らない。ただし記録は残す
            "suppressed_by_ai": True,    # 呼び出し側は診断へ残し、通知はしない
            "confidence": confidence,
            "perpetrator": perpetrator,
            "hotline_key": (py_result or {}).get("hotline_key"),
            "uncertain": False,
            "matched": (py_result or {}).get("matched", ""),
            "ai_reason": ai.get("reason", ""),
            "detected_by": "python_only_ai_denied",
        }

    order = {"urgent": 0, "high": 1, "medium": 2}
    ai_urg = ai.get("urgency")
    py_urg = (py_result or {}).get("urgency")
    # 切迫度は高いほうを採る（見積もりを甘くしない）
    cands = [u for u in (ai_urg, py_urg) if u in order]
    urgency = min(cands, key=lambda u: order[u]) if cands else "high"

    # カテゴリは、虐待が絡むなら虐待を優先する（親通知を止める側へ倒すため）。
    # AI が「加害者は家族」と見たら、カテゴリ名に関わらず虐待として扱う（bullying と誤ラベルされても救う）。
    if ai_cat == "abuse" or py_cat == "abuse" or perpetrator == "family":
        category = "abuse"
    else:
        category = ai_cat or py_cat

    # 親へ通知してよいか。次のいずれかに当たれば送らない（安全側へ倒す）。
    #   ・虐待の可能性がある（加害者が親でありうる）
    #   ・AI が uncertain と言った／加害者が unknown（家族の可能性を否定できない）
    # なお暴力被害でない類型（喫煙・依存等）は加害者の概念が無いため unknown でも送ってよい。
    uncertain = bool(ai.get("uncertain"))
    victim_types = ("bullying", "grooming")   # 加害者が存在する類型
    blocked = (
        category == "abuse"
        or uncertain
        or (category in victim_types and perpetrator == "unknown")
    )
    notify_parent = not blocked

    # 窓口は統合後のカテゴリから引く（Python 側の対応表を正とする）
    hotline = None
    for name, _pats, _u, _n, hk in _CATEGORIES:
        if name == category:
            hotline = hk
            break

    return {
        "category": category,
        "urgency": urgency,
        "notify_parent": notify_parent,
        "hotline_key": hotline,
        "uncertain": uncertain,
        "confidence": confidence,
        "perpetrator": perpetrator,
        "matched": (py_result or {}).get("matched", ""),
        "ai_reason": ai.get("reason", ""),
        "detected_by": ("both" if (ai_cat and py_cat) else ("ai" if ai_cat else "python")),
    }


# カテゴリの日本語表示。親への通知で「何が起きているか」を一目で伝える
_CATEGORY_LABEL = {
    "self_harm": "自分を傷つけたい気持ち・つらさの訴え",
    "bullying": "いじめ・暴力被害の訴え",
    "grooming": "性的な誘い・見知らぬ人からの接触",
    "illegal_work": "違法な稼ぎ方への関心",
    "eating": "食事や体型に関する気になる訴え",
    "substance": "喫煙・飲酒・薬物への関心",
    "abuse": "家庭内の安全に関わる訴え",
}

_URGENCY_LABEL = {
    "urgent": "【緊急】すぐに確認してください",
    "high": "【重要】早めに確認してください",
    "medium": "【参考】気に留めておいてください",
}

# 第三者の実名らしき箇所を伏せるためのパターン。
# 原文をそのまま親へ渡すと加害児童の実名が残り、親が学校へ乗り込む等、子が最も恐れる展開を招く。
_NAME_LIKE = re.compile(
    r"([一-龥ぁ-んァ-ヶA-Za-z]{1,6})(くん|君|ちゃん|さん|先輩|後輩)"
)


def redact_third_party(text: str) -> str:
    """原文から第三者の実名らしき箇所を伏せる（親へ渡す前の加工）。

    「○○くんに殴られた」の実名が親チャンネルに残ると、親が相手の家庭や学校へ直接動き、
    子が最も恐れる展開（事が大きくなる・報復される）を招く。関係性は残しつつ名前だけ伏せる。

    Args:
        text: 子の発話原文。

    Returns:
        str: 実名を伏せた文字列。
    """
    return _NAME_LIKE.sub(lambda m: f"（同じくらいの子）{m.group(2)}", text or "")


def build_parent_notification(child_name: str, judgment: dict, raw_text: str,
                              child_consent: str = "unknown") -> str:
    """親チャンネルへ送る通知文を組み立てる（①要約 → ②背景 → ③原文の順）。

    要約と背景を先に置くのは、親が**重要度と緊急性を誤判断しない**ようにするため。
    原文を添えるのは、要約だけでは親が緊急性を測れず、AI の要約バイアスで深刻度が落ちるため。
    ただし原文は当該発言に限定し、第三者の実名は伏せる。

    性的な話題は原文を載せず性質の記述に留める。原文直送は子を辱め、
    「恥じさせない」という会話側の方針と正面から矛盾するためである。

    Args:
        child_name: 対象児童の名前。
        judgment: merge_judgments の結果。
        raw_text: 子の発話原文。
        child_consent: 子の同意状況。"agreed"（伝えていいと言った）/
            "refused"（知らせたくないと言った）/"unknown"（確認できていない）。

    Returns:
        str: 親チャンネルへ送る本文。
    """
    category = judgment.get("category", "")
    urgency = judgment.get("urgency", "high")
    header = _URGENCY_LABEL.get(urgency, _URGENCY_LABEL["high"])
    label = _CATEGORY_LABEL.get(category, "気になる訴え")

    lines = [
        f"{header}",
        f"**{child_name} さんとの会話で、{label}がありました。**",
        "",
        "**■ どういう状況か**",
        f"{judgment.get('ai_reason') or '会話の中に気になる内容がありました。'}",
    ]

    # 確信度が低いときは、親が過剰に反応しないよう正直に添える
    conf = judgment.get("confidence")
    if isinstance(conf, (int, float)) and conf < 0.7:
        lines.append("※ AI の判断としては確信が高くありません。言葉のあやの可能性もあります。")

    # 子の同意状況。「知らせたくない」と言っている場合は必ず伝え、親が問い詰めないよう促す
    lines.append("")
    lines.append("**■ 本人の意向**")
    if child_consent == "agreed":
        lines.append("本人は「おうちの人に伝えていい」と言っています。")
    elif child_consent == "refused":
        lines.append(
            "**本人は「知らせたくない」と言っています。** それでも安全に関わるためお伝えしています。\n"
            "いきなり問い詰めると、次から話してくれなくなります。"
            "本人が話し出すのを待つか、「何かあったら聞くよ」と伝えるだけに留めてください。"
        )
    else:
        lines.append(
            "本人への確認は取れていません（返事がない・会話が途切れた等）。\n"
            "いきなり問い詰めず、様子を見ながら声をかけてください。"
        )

    # 原文。性的な話題は載せず性質の記述に留める（子を辱めない）
    lines.append("")
    lines.append("**■ 本人の言葉**")
    if category == "grooming":
        lines.append("（内容の性質上、原文は載せていません。性的な誘いや接触に関する内容でした）")
    else:
        safe = redact_third_party(raw_text or "").strip()
        # 原文は当該発言のみ。長すぎる場合は先頭のみ載せる（会話全体を貼らない）
        if len(safe) > 200:
            safe = safe[:200] + "…"
        lines.append(f"> {safe}" if safe else "（記録できませんでした）")

    return "\n".join(lines)


def build_child_hotline_message(judgment: dict) -> str:
    """虐待の疑いがある場合に、子へ直接渡す外部窓口の案内を組み立てる。

    親チャンネルへは送らないため、この案内が子にとって唯一の外部への導線になる。
    「おうちの人に言う」を提案せず、子が親を介さず単独で到達できる窓口だけを渡す。

    Args:
        judgment: merge_judgments の結果。

    Returns:
        str: 子へ送る案内文。
    """
    urgent = judgment.get("urgency") == "urgent"
    body = hotlines_for(judgment.get("hotline_key") or "abuse", urgent=urgent)
    return (
        "話してくれてありがとう。それ、ひとりで抱えるにはしんどいことだと思う。\n"
        "ここで話したことを、そのままおうちの人に伝えることはしないよ。\n\n"
        "もしよかったら、こういうところにも話してみてほしいんだ。"
        "きみの味方になってくれる人たちだよ。\n"
        f"{body}\n\n"
        "学校の先生や保健室の先生、親戚の人でもいい。"
        "話せそうな人、だれか思いつくかな？"
    )


def hotlines_for(hotline_key: str | None, urgent: bool = False) -> str:
    """子へ渡す公的窓口の案内文を組み立てる（LLM に番号を生成させないための定数経由）。

    Args:
        hotline_key: HOTLINES のキー。None なら窓口を出さない。
        urgent: 切迫している場合は緊急通報先も添える。

    Returns:
        str: 案内文。hotline_key が無ければ空文字。
    """
    if not hotline_key:
        return ""
    lines = HOTLINES.get(hotline_key) or []
    if not lines:
        return ""
    body = "\n".join(f"・{x}" for x in lines)
    tail = f"\n{EMERGENCY_LINE}" if urgent else ""
    return f"{body}{tail}"
