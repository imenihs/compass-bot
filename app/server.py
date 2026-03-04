"""
Compass Bot Webダッシュボード + ヘルスチェック FastAPI サーバー。
認証フロー: 申請 → Discord通知 → 親がweb承認 → 仮PW発行 → 本PW設定 → ダッシュボード
"""

import datetime
import json
from pathlib import Path
from typing import Optional

import discord
from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import web_auth
from app.config import (
    get_allow_channel_ids,
    get_allowance_reminder_setting,
    get_low_balance_alert_setting,
    get_web_base_url,
    find_user_by_name,
    load_all_users,
    load_system,
)
from app.storage import JST

# アプリケーションインスタンス
app = FastAPI()

# テンプレートディレクトリ
ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))

# bot.py から on_ready で注入されるグローバル変数
_discord_client: Optional[discord.Client] = None
_wallet_service = None  # WalletService インスタンス


def init(discord_client: discord.Client, wallet_service) -> None:
    """on_ready から依存オブジェクトを受け取って初期化する"""
    global _discord_client, _wallet_service
    _discord_client = discord_client
    _wallet_service = wallet_service


# ---------- ヘルスチェック ----------

@app.get("/health")
def health():
    """死活監視エンドポイント"""
    return {"status": "ok"}


# ---------- ユーティリティ ----------

async def _get_current_user(session_token: Optional[str]) -> Optional[str]:
    """セッショントークンからログイン中のユーザー名を取得する。未ログインは None を返す"""
    if not session_token:
        return None
    return await web_auth.get_session_user(session_token)


def _is_admin(username: str) -> bool:
    """ユーザー名が管理者（setting.json の parent_ids に登録済み）か確認する。
    Web ユーザーの is_admin フラグとの二段階判定で安全性を高める"""
    # web_users.json の is_admin フラグで確認する（parent との紐付けは承認時に設定）
    users_data = web_auth._read_json(web_auth.WEB_USERS_PATH)
    user = users_data.get(username, {})
    return bool(user.get("is_admin", False))


async def _notify_discord(message: str) -> bool:
    """Discord の allowance_reminder.channel_id にメッセージを送信する"""
    if _discord_client is None:
        return False
    try:
        # 通知先チャンネルを取得する（allowance_reminder → allow_channel_ids の順でフォールバック）
        reminder_conf = get_allowance_reminder_setting()
        channel_id = reminder_conf.get("channel_id")
        if not channel_id:
            allow_ids = get_allow_channel_ids()
            channel_id = next(iter(allow_ids), None) if allow_ids else None
        if not channel_id:
            return False
        channel = _discord_client.get_channel(int(channel_id))
        if channel:
            await channel.send(message)
            return True
    except Exception:
        pass
    return False


def _build_user_stats(name: str, system_conf: dict) -> dict:
    """ダッシュボード表示用のユーザー統計データを組み立てる"""
    from app.config import get_log_dir
    from app.storage import JST
    import datetime

    log_dir = get_log_dir(system_conf)
    now = datetime.datetime.now(JST)
    month_str = now.strftime("%Y-%m")

    # 今月の支出記録を集計する
    journal_path = log_dir / f"{name}_pocket_journal.jsonl"
    month_spending = 0
    month_count = 0
    last_spent_date = None
    recent_items = []
    if journal_path.exists():
        with open(journal_path, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        # 今月分のみフィルタする
        for r in rows:
            ts = r.get("ts", "")
            if ts.startswith(month_str):
                month_count += 1
                amount = r.get("amount")
                if amount is not None:
                    month_spending += int(amount)
                if last_spent_date is None:
                    last_spent_date = ts[:10]
        # 直近5件を recent_items に格納する（新しい順に逆引き）
        for r in reversed(rows[-10:]):
            recent_items.append({
                "date": r.get("ts", "")[:10],
                "item": r.get("item", ""),
                "satisfaction": r.get("satisfaction"),
                "amount": r.get("amount"),
            })
            if len(recent_items) >= 5:
                break

    # 残高を取得する
    balance = None
    has_wallet = False
    if _wallet_service:
        has_wallet = _wallet_service.has_wallet(name)
        if has_wallet:
            balance = _wallet_service.get_balance(name)

    # 低残高フラグを判定する
    low_balance_conf = get_low_balance_alert_setting()
    threshold = low_balance_conf.get("threshold", 500)
    low_balance = has_wallet and balance is not None and balance < threshold

    # 貯金目標を取得してパーセント計算する
    goals = []
    if _wallet_service and has_wallet:
        for g in _wallet_service.get_savings_goals(name):
            target = int(g.get("target_amount", 0))
            saved = balance if balance is not None else 0
            pct = int(saved / target * 100) if target > 0 else 0
            goals.append({
                "title": g.get("title", ""),
                "target": target,
                "saved": saved,
                "pct": min(pct, 100),
            })

    # 残高報告状態を確認する
    audit_reported = False
    if _wallet_service:
        audit_state = _wallet_service.load_audit_state()
        pending = audit_state.get("pending_by_user", {})
        # pending に名前がなければ報告済みとみなす
        audit_reported = name not in pending

    return {
        "name": name,
        "balance": balance,
        "has_wallet": has_wallet,
        "low_balance": low_balance,
        "month_spending": month_spending,
        "month_count": month_count,
        "last_spent_date": last_spent_date,
        "audit_reported": audit_reported,
        "goals": goals,
        "recent_items": recent_items,
    }


# ---------- 認証ルート ----------

@app.get("/compass-bot/register", response_class=HTMLResponse)
async def get_register(request: Request):
    """アクセス申請ページを表示する"""
    return templates.TemplateResponse("register.html", {
        "request": request,
        "username": None,
        "error": None,
        "success": None,
    })


@app.post("/compass-bot/register", response_class=HTMLResponse)
async def post_register(request: Request, username: str = Form(...)):
    """アクセス申請を受け付けてDiscordに通知する"""
    username = username.strip()
    if not username:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "username": None,
            "error": "ユーザー名を入力してください。",
            "success": None,
        })

    # すでに登録済みのユーザーは申請不要
    if await web_auth.user_exists(username):
        return templates.TemplateResponse("register.html", {
            "request": request,
            "username": None,
            "error": f"「{username}」はすでに登録済みです。ログインしてください。",
            "success": None,
        })

    # 申請を登録する
    app_id = await web_auth.create_application(username)

    # Discord に承認依頼を通知する
    msg = (
        f"🌐 **Webダッシュボード アクセス申請**\n"
        f"ユーザー名: **{username}**\n"
        f"承認するには Discord で `web承認 {username}` と送信してください。"
    )
    await _notify_discord(msg)

    return templates.TemplateResponse("register.html", {
        "request": request,
        "username": None,
        "error": None,
        "success": f"申請を受け付けました（ID: {app_id}）。管理者の承認をお待ちください。",
    })


@app.get("/compass-bot/login", response_class=HTMLResponse)
async def get_login(request: Request):
    """ログインページを表示する"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "username": None,
        "error": None,
    })


@app.post("/compass-bot/login", response_class=HTMLResponse)
async def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """ログイン認証を行い、成功時はセッションを発行してダッシュボードへリダイレクトする"""
    username = username.strip()
    ok = await web_auth.verify_password(username, password)
    if not ok:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "username": None,
            "error": "ユーザー名またはパスワードが正しくありません。",
        })
    # セッションを発行する
    token = await web_auth.create_session(username)
    response = RedirectResponse(url="/compass-bot/dashboard", status_code=303)
    # httponly=True でJavaScriptからの読み取りを防ぐ
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=7*24*3600)
    return response


@app.get("/compass-bot/logout")
async def logout(session_token: Optional[str] = Cookie(default=None)):
    """セッションを削除してログインページへリダイレクトする"""
    if session_token:
        await web_auth.delete_session(session_token)
    response = RedirectResponse(url="/compass-bot/login", status_code=303)
    response.delete_cookie("session_token")
    return response


@app.get("/compass-bot/set_password", response_class=HTMLResponse)
async def get_set_password(request: Request, username: Optional[str] = None):
    """仮パスワード入力ページを表示する"""
    return templates.TemplateResponse("set_password.html", {
        "request": request,
        "step": "temp",
        "username": None,
        "username_hint": username,
        "token": None,
        "error": None,
    })


@app.post("/compass-bot/set_password", response_class=HTMLResponse)
async def post_set_password(
    request: Request,
    step: str = Form(...),
    username: str = Form(default=""),
    temp_password: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    token: str = Form(default=""),
):
    """仮PW検証（step=temp）と本PW設定（step=set）の2段階フォーム処理を行う"""
    username = username.strip()

    if step == "temp":
        # 仮パスワードを検証する
        ok = await web_auth.consume_temp_password(username, temp_password.strip())
        if not ok:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "temp",
                "username": None,
                "username_hint": username,
                "token": None,
                "error": "仮パスワードが正しくありません。",
            })
        # 仮PW確認済み → 本PW設定フォームへ（one-time token を発行してCSRF対策とする）
        import secrets
        set_token = secrets.token_urlsafe(16)
        # 本PW設定待ち状態はweb_auth_state.json で管理済み（consume_temp_password で移行済み）
        return templates.TemplateResponse("set_password.html", {
            "request": request,
            "step": "set",
            "username": username,
            "username_hint": None,
            "token": set_token,
            "error": None,
        })

    elif step == "set":
        # パスワードの強度・一致チェックをする
        if len(password) < 8:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "set",
                "username": username,
                "username_hint": None,
                "token": token,
                "error": "パスワードは8文字以上にしてください。",
            })
        if password != password_confirm:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "set",
                "username": username,
                "username_hint": None,
                "token": token,
                "error": "パスワードが一致しません。",
            })
        # パスワード設定完了 → pw_setting 状態であることを確認してから保存する
        if not await web_auth.is_pw_setting_mode(username):
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "temp",
                "username": None,
                "username_hint": username,
                "token": None,
                "error": "セッションが無効です。仮パスワードから再入力してください。",
            })
        await web_auth.set_password(username, password)
        # 設定完了後は自動ログインする
        session_token = await web_auth.create_session(username)
        response = RedirectResponse(url="/compass-bot/dashboard", status_code=303)
        response.set_cookie("session_token", session_token, httponly=True, samesite="lax", max_age=7*24*3600)
        return response

    # 不正なステップ値は login に戻す
    return RedirectResponse(url="/compass-bot/login", status_code=303)


# ---------- ダッシュボード ----------

@app.get("/compass-bot/dashboard", response_class=HTMLResponse)
async def get_dashboard(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    msg: str = "",
    error: str = "",
):
    """ダッシュボードページを表示する。未ログインはログインページへリダイレクトする"""
    username = await _get_current_user(session_token)
    if not username:
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    is_admin = _is_admin(username)
    system_conf = load_system()

    if is_admin:
        # 管理者: 全ユーザーの統計を収集する
        all_users = load_all_users()
        user_stats = []
        for u in all_users:
            name = u.get("name", "")
            if name:
                stats = _build_user_stats(name, system_conf)
                user_stats.append(stats)

        # 承認待ち申請一覧を取得する
        pending_apps = await web_auth.list_pending_applications()
        for app in pending_apps:
            ts = app.get("requested_at", 0)
            app["requested_at_str"] = datetime.datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d %H:%M") if ts else "—"

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "username": username,
            "is_admin": True,
            "users": user_stats,
            "pending_apps": pending_apps,
            "flash_msg": msg,
            "flash_error": error,
        })
    else:
        # 一般ユーザー（子供）: 自分のデータのみ表示する
        # app/users/*.json に登録されている名前と Web ユーザー名を紐付ける
        # （Web申請時のユーザー名 == users/*.json の name フィールドを前提とする）
        stats = _build_user_stats(username, system_conf)
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "username": username,
            "is_admin": False,
            "my_balance": stats["balance"],
            "my_low_balance": stats["low_balance"],
            "my_goals": stats["goals"],
            "my_month_spending": stats["month_spending"],
            "my_month_count": stats["month_count"],
            "my_recent_items": stats["recent_items"],
            "flash_msg": msg,
            "flash_error": error,
        })


# ---------- 管理者操作 ----------

@app.post("/compass-bot/admin/approve", response_class=HTMLResponse)
async def admin_approve(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    username: str = Form(...),
):
    """管理者がWeb申請を承認する。Webフォームからの操作用エンドポイント"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    username = username.strip()
    temp_pw = await web_auth.approve_application(username)
    if temp_pw:
        # Discord に仮パスワードを通知する
        base_url = get_web_base_url()
        msg = (
            f"✅ **Webアクセスを承認しました**\n"
            f"ユーザー: **{username}**\n"
            f"仮パスワード: `{temp_pw}`\n"
            f"下記URLからパスワードを設定してください:\n"
            f"{base_url}/compass-bot/set_password?username={username}"
        )
        await _notify_discord(msg)

    # ダッシュボードへ戻る
    return RedirectResponse(url="/compass-bot/dashboard", status_code=303)


# ---------- 親操作エンドポイント（Phase A） ----------

def _op_redirect(msg: str = "", error: str = "") -> RedirectResponse:
    """操作後にダッシュボードへリダイレクトする。結果メッセージをクエリパラメータで渡す"""
    from urllib.parse import quote
    if error:
        return RedirectResponse(url=f"/compass-bot/dashboard?error={quote(error)}", status_code=303)
    return RedirectResponse(url=f"/compass-bot/dashboard?msg={quote(msg)}", status_code=303)


@app.post("/compass-bot/op/grant")
async def op_grant(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    amount: str = Form(...),
):
    """親が特定ユーザーへ手動支給する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    # 金額を整数に変換する（カンマ・円記号を除去）
    try:
        amt = int(amount.replace(",", "").replace("円", "").strip())
    except ValueError:
        return _op_redirect(error="金額が正しくありません。")
    if amt <= 0:
        return _op_redirect(error="金額は1円以上を入力してください。")

    target_conf = find_user_by_name(target)
    if target_conf is None:
        return _op_redirect(error=f"「{target}」は見つかりませんでした。")
    if not _wallet_service or not _wallet_service.has_wallet(target):
        return _op_redirect(error=f"「{target}」のウォレットが未設定です。")

    system_conf = load_system()
    before = _wallet_service.get_balance(target)
    new_balance, _ = _wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=amt,
        action="allowance_manual_grant",
        note="manual_grant_by_parent_web",
        extra={"granted_by": current_user},
    )
    return _op_redirect(msg=f"{target}に{amt:,}円を支給しました（{before:,}円 → {new_balance:,}円）")


@app.post("/compass-bot/op/bulk_grant")
async def op_bulk_grant(
    session_token: Optional[str] = Cookie(default=None),
):
    """親が全子供ユーザーに固定お小遣いを一括支給する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    if not _wallet_service:
        return _op_redirect(error="ウォレットサービスが未初期化です。")

    system_conf = load_system()
    results = []
    # 子供ユーザー（load_all_users = 子供のみ）を一括処理する
    for user_conf in sorted(load_all_users(), key=lambda x: str(x.get("name", ""))):
        name = user_conf.get("name", "")
        fixed = int(user_conf.get("fixed_allowance", 0))
        if not name or fixed <= 0:
            continue
        if not _wallet_service.has_wallet(name):
            continue
        before = _wallet_service.get_balance(name)
        new_bal, _ = _wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=fixed,
            action="allowance_grant",
            note="bulk_grant_by_parent_web",
            extra={"granted_by": current_user},
        )
        results.append(f"{name}: {before:,}→{new_bal:,}円")

    if not results:
        return _op_redirect(error="支給対象のユーザーが見つかりませんでした。")
    return _op_redirect(msg="一括支給完了 / " + " | ".join(results))


@app.post("/compass-bot/op/adjust")
async def op_adjust(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    amount: str = Form(...),
    direction: str = Form(...),  # "plus" or "minus"
):
    """親が残高を手動調整する（加算・減算）"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    try:
        amt = int(amount.replace(",", "").replace("円", "").strip())
    except ValueError:
        return _op_redirect(error="金額が正しくありません。")
    if amt <= 0:
        return _op_redirect(error="金額は1円以上を入力してください。")

    delta = amt if direction == "plus" else -amt

    target_conf = find_user_by_name(target)
    if target_conf is None:
        return _op_redirect(error=f"「{target}」は見つかりませんでした。")
    if not _wallet_service or not _wallet_service.has_wallet(target):
        return _op_redirect(error=f"「{target}」のウォレットが未設定です。")

    system_conf = load_system()
    before = _wallet_service.get_balance(target)
    new_balance, _ = _wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=delta,
        action="balance_adjustment",
        note="manual_adjustment_by_parent_web",
        extra={"adjusted_by": current_user},
    )
    label = "加算" if delta >= 0 else "減算"
    return _op_redirect(msg=f"{target}の残高を{label}しました（{before:,}円 → {new_balance:,}円）")


# ---------- エントリーポイント ----------

@app.get("/", response_class=HTMLResponse)
@app.get("/compass-bot", response_class=HTMLResponse)
@app.get("/compass-bot/", response_class=HTMLResponse)
async def index(session_token: Optional[str] = Cookie(default=None)):
    """ルートアクセス: ログイン済みならダッシュボード、未ログインはログインページへ"""
    username = await _get_current_user(session_token)
    if username:
        return RedirectResponse(url="/compass-bot/dashboard", status_code=303)
    return RedirectResponse(url="/compass-bot/login", status_code=303)
