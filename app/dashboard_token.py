"""ダッシュボードの UUID トークン管理（docs/設計_UUID認証方式.md）。

パスワード方式は子どもには重すぎた（申請→親承認→仮PW→本PW設定→7日ごと再ログイン）。
社長の指示「uuid みたいな子ども専用URLを発行し、それを使い続けるくらいでいいよ」に沿って、
**URL を開くだけで入れる**方式へ移す。

設計の要点:
  ・**キーは user_key（`child:<ファイル名>` / `parent:<ファイル名>`）**。
    `discord_user_id` は使えない。実データで**子「テスト」と親「とうちゃん」が同一 ID**であり
    （bot.py に「兼務アカウント」として明示的に扱われている既知の運用）、
    ID をキーにすると子トークンと親トークンが上書きし合う。
    ファイル名は名前変更（op_user_settings）の影響を受けないため安定している。
  ・**URL に UUID を残さない**。初回だけ `/child/<uuid>` を開き、
    Cookie を置いて UUID 無しの URL へリダイレクトする。
    これで Apache のアクセスログ・ブラウザ履歴・スクショ・Referer が一度に塞がる。
  ・**再発行は Discord から**。Web に入口を置くと、盗んだ側も再発行できてしまい
    正規の本人が締め出される（初稿の設計はこの循環を抱えていた）。

移行は二段階で行う。前半で UUID を**追加**し（パスワード方式は残す）、
全員が入れることを確認してから後半で廃止する。
先に廃止すると、UUID が行き渡る前に誰も管理画面へ入れなくなる。
"""
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from app.storage import now_jst_iso

ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = ROOT / "data" / "dashboard_tokens.json"

# トークンは1人1つ。再発行すると古いものは即無効になる
_LOCK = threading.RLock()

ROLE_CHILD = "child"
ROLE_PARENT = "parent"


def _interprocess_lock():
    """プロセス間ロック。bot と web が同じファイルを読み書きするため必須。"""
    from app.wallet_service import _interprocess_lock as _lk
    return _lk(TOKENS_PATH.with_suffix(".json.lock"))


def _load() -> dict:
    """トークン一覧を読む。壊れていても落とさず空で返す。

    ここで例外を出すと**ダッシュボードに誰も入れなくなる**ため、
    読めない場合は空として扱い、再発行で復旧できる状態にする。
    """
    try:
        if not TOKENS_PATH.exists():
            return {}
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(doc: dict) -> None:
    """一時ファイル＋置換で保存する（書き込み途中の破損を避ける）。"""
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(TOKENS_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        Path(tmp).replace(TOKENS_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_user_key(role: str, file_stem: str) -> str:
    """user_key を組み立てる。

    Args:
        role: ROLE_CHILD / ROLE_PARENT。
        file_stem: 設定ファイル名（拡張子なし）。例 "test" / "akira"。

    Returns:
        str: 例 "child:test" / "parent:akira"。
    """
    return f"{str(role).strip()}:{str(file_stem).strip()}"


def issue(user_key: str, role: str, issued_by: str = "") -> str:
    """トークンを発行する。**同じ user_key の古いトークンは即無効**にする。

    再発行もこの関数で行う。古いものを残すと、盗まれた URL が生き続ける。

    Args:
        user_key: build_user_key() で作ったキー。
        role: ROLE_CHILD / ROLE_PARENT。画面と権限を決める。
        issued_by: 発行を実行した discord_user_id（記録用）。

    Returns:
        str: 新しい UUID。
    """
    token = uuid.uuid4().hex
    ts = now_jst_iso()
    with _LOCK, _interprocess_lock():
        doc = _load()
        # 同じ user_key の既存トークンを失効させる（1人1つ）
        for existing, meta in doc.items():
            if isinstance(meta, dict) and meta.get("user_key") == user_key \
                    and not meta.get("revoked"):
                meta["revoked"] = True
                meta["revoked_at"] = ts
                meta["revoked_by"] = str(issued_by or "")
        doc[token] = {
            "user_key": user_key,
            "role": str(role),
            "created": ts,
            "revoked": False,
            "revoked_at": None,
            "revoked_by": None,
        }
        _save(doc)
    return token


def resolve(token: str) -> dict | None:
    """トークンから利用者を解決する。無効なら None。

    **未登録・失効したトークンは必ず None を返す**（社長指示「未承認のUUIDははじく」）。

    Args:
        token: URL または Cookie から受け取った UUID。

    Returns:
        dict | None: {"user_key", "role"}。無効なら None。
    """
    key = str(token or "").strip()
    if not key:
        return None
    with _LOCK, _interprocess_lock():
        meta = _load().get(key)
    if not isinstance(meta, dict) or meta.get("revoked"):
        return None
    return {"user_key": str(meta.get("user_key", "")), "role": str(meta.get("role", ""))}


def find_active_token(user_key: str) -> str | None:
    """user_key に対する有効なトークンを返す。無ければ None。

    「もう発行済みか」を確認して二重発行を避けるために使う。

    Args:
        user_key: build_user_key() で作ったキー。

    Returns:
        str | None: 有効な UUID。
    """
    with _LOCK, _interprocess_lock():
        doc = _load()
    for token, meta in doc.items():
        if isinstance(meta, dict) and meta.get("user_key") == user_key \
                and not meta.get("revoked"):
            return token
    return None


def split_user_key(user_key: str) -> tuple[str, str]:
    """user_key を (role, file_stem) に分解する。

    Args:
        user_key: "child:test" のような文字列。

    Returns:
        tuple[str, str]: (role, file_stem)。壊れていれば ("", "")。
    """
    raw = str(user_key or "")
    if ":" not in raw:
        return "", ""
    role, _, stem = raw.partition(":")
    return role.strip(), stem.strip()


# DM送信の要求キュー。
# tool は別プロセス（mcp_wallet）で動き Discord を持たないため、
# 「この人へURLをDMして」をファイルへ積み、bot プロセスが拾って送る。
# parent_confirm で同じ問題を踏んでおり、そのときと同じくファイル＋flock を使う。
DM_QUEUE_PATH = ROOT / "data" / "dashboard_dm_queue.json"
# bot プロセスにしかできない送信処理（一斉通知・安全設定チェック）の依頼キュー
ACTION_QUEUE_PATH = ROOT / "data" / "bot_action_queue.json"
# 依頼キューの上限。毎ターン消化するので通常は数件。異常時の暴走を止めるためだけの値
_ACTION_QUEUE_MAX = 50


def _queue_lock(path: Path):
    """キュー用のプロセス間ロック。**キューごとに別のロックを取る**。

    当初は1つのロックを DM とアクションで共用していた。壊れはしないが、
    ロック名が実態と食い違い（DM 用のロックがアクションを守っている）、
    後から片方に専用ロックを足された瞬間に無防備になる。名前と対象を一致させる。

    Args:
        path: 守るキューファイルのパス。

    Returns:
        プロセス間ロックのコンテキストマネージャ。
    """
    from app.wallet_service import _interprocess_lock as _lk
    return _lk(path.with_suffix(".json.lock"))


def request_bot_action(kind: str, payload: dict) -> None:
    """「bot プロセスにしかできない処理」を依頼する（tool から呼ぶ）。

    tool は Discord を持たない別プロセスなので、送信系は自分で実行できない。
    ここへ積み、bot プロセスが take_bot_actions() で拾って実行する。

    Args:
        kind: 依頼の種類（broadcast_usage_guide / safety_setup_check）。
        payload: 依頼ごとの引数。
    """
    with _LOCK, _queue_lock(ACTION_QUEUE_PATH):
        try:
            doc = json.loads(ACTION_QUEUE_PATH.read_text(encoding="utf-8")) \
                if ACTION_QUEUE_PATH.exists() else []
            if not isinstance(doc, list):
                doc = []
        except (OSError, json.JSONDecodeError):
            doc = []
        doc.append({"kind": str(kind), "payload": payload or {}, "ts": now_jst_iso()})
        # 暴走時の歯止めとして上限を持つが、**捨てたことは必ず残す**。
        # 毎ターン消化するので通常は数件で、ここに引っかかるのは異常時だけ。
        # 無言で正当な依頼を落とすと「頼んだのに何も起きない」になる
        if len(doc) > _ACTION_QUEUE_MAX:
            dropped = len(doc) - _ACTION_QUEUE_MAX
            doc = doc[-_ACTION_QUEUE_MAX:]
            print(f"[dashboard_token] action queue overflow: dropped {dropped} request(s)")
        ACTION_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(ACTION_QUEUE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            Path(tmp).replace(ACTION_QUEUE_PATH)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


def take_bot_actions() -> list[dict]:
    """積まれた依頼を取り出してキューを空にする（bot プロセスが呼ぶ）。

    Returns:
        list[dict]: [{"kind": str, "payload": dict}, ...] 積まれた順。
    """
    with _LOCK, _queue_lock(ACTION_QUEUE_PATH):
        try:
            doc = json.loads(ACTION_QUEUE_PATH.read_text(encoding="utf-8")) \
                if ACTION_QUEUE_PATH.exists() else []
            if not isinstance(doc, list) or not doc:
                return []
        except (OSError, json.JSONDecodeError):
            return []
        try:
            ACTION_QUEUE_PATH.write_text("[]\n", encoding="utf-8")
        except OSError:
            pass
    return [x for x in doc if isinstance(x, dict) and x.get("kind")]


def request_dm(discord_user_id: int, user_key: str, role: str) -> None:
    """「この人へダッシュボードURLをDMして」と積む（tool から呼ぶ）。

    tool は Discord を持たない別プロセスなので、自分では送れない。
    bot プロセスが take_dm_requests() で拾って送る。

    同じ人の要求が既にあれば上書きする（連打しても1通だけ届く）。

    Args:
        discord_user_id: 送り先の Discord ID。
        user_key: どのダッシュボードか（child:test / parent:akira）。
        role: child / parent。
    """
    with _LOCK, _queue_lock(DM_QUEUE_PATH):
        try:
            doc = json.loads(DM_QUEUE_PATH.read_text(encoding="utf-8")) \
                if DM_QUEUE_PATH.exists() else {}
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, json.JSONDecodeError):
            doc = {}
        # **キーは (Discord ID, user_key) の組**。ID だけにすると兼務アカウント
        # （同じ人が親としても子としても登録されている。実データに存在する）で
        # 子の要求が親の要求に上書きされ、子のURLが永久に届かなくなる。
        # 同じ組の連打は上書きしてよい（1通だけ届く）。
        doc[f"{int(discord_user_id)}\t{user_key}"] = {
            "discord_user_id": int(discord_user_id),
            "user_key": user_key, "role": role, "ts": now_jst_iso(),
        }
        DM_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(DM_QUEUE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            Path(tmp).replace(DM_QUEUE_PATH)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


def take_dm_requests() -> list[dict]:
    """積まれたDM要求を取り出してキューを空にする（bot プロセスが呼ぶ）。

    Returns:
        list[dict]: [{"discord_user_id": int, "user_key": str, "role": str}, ...]
    """
    with _LOCK, _queue_lock(DM_QUEUE_PATH):
        try:
            doc = json.loads(DM_QUEUE_PATH.read_text(encoding="utf-8")) \
                if DM_QUEUE_PATH.exists() else {}
            if not isinstance(doc, dict) or not doc:
                return []
        except (OSError, json.JSONDecodeError):
            return []
        try:
            DM_QUEUE_PATH.write_text("{}\n", encoding="utf-8")
        except OSError:
            pass
    out = []
    for key, meta in doc.items():
        if not isinstance(meta, dict):
            continue
        try:
            # 旧形式（キーが ID だけ・値に discord_user_id が無い）も読めるようにする。
            # 移行時にキューへ残っていた依頼を落とさないため
            uid = meta.get("discord_user_id", str(key).split("\t")[0])
            out.append({
                "discord_user_id": int(uid),
                "user_key": str(meta.get("user_key", "")),
                "role": str(meta.get("role", "")),
            })
        except (TypeError, ValueError):
            continue
    return out
