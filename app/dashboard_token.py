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
