"""Phase N-11 r10 blocker回帰防止: 親の自然文ブロック判定が越境を確実に止めるか。

r9でブロック条件を find_user_by_discord_id(=親優先で親を返す)に緩めた結果、純粋な親でも
『子として実在』判定になりブロックが不発になり、親の自然文で子残高が動く越境が復活した。
r10で find_child_user_by_discord_id(子ディレクトリのみ)に直した。この判定の核を検証する:
  - 純粋な親: 子として実在しない → ブロックされるべき(越境阻止)
  - 親兼子アカウント(親IDが子にも登録): 子として実在する → 通すべき(会話可)
  - 子本人: 子として実在する → 通すべき

隔離ディレクトリで実データに触れない。結果は1行1 JSON で出力し集計する。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config

_results: list[dict] = []


def _check(name: str, passed: bool, detail: str = "") -> None:
    _results.append({"test": name, "passed": bool(passed), "detail": detail})


def _setup(tmp: Path) -> None:
    """子2人(たろう・兼務くん)と親2人(純粋親・兼務くんと同ID)を隔離配置する。"""
    (tmp / "settings" / "users" / "parents").mkdir(parents=True, exist_ok=True)
    # 子: たろう(純粋な子, id=111)
    (tmp / "settings" / "users" / "tarou.json").write_text(
        json.dumps({"name": "たろう", "age": 10, "discord_user_id": 111}, ensure_ascii=False), encoding="utf-8"
    )
    # 子: 兼務くん(子として登録, id=555) ← 親にも同IDが居る兼務アカウント
    (tmp / "settings" / "users" / "kenmu.json").write_text(
        json.dumps({"name": "兼務くん", "age": 11, "discord_user_id": 555}, ensure_ascii=False), encoding="utf-8"
    )
    # 親: 純粋な親(id=999) ← 子には居ない
    (tmp / "settings" / "users" / "parents" / "oya.json").write_text(
        json.dumps({"name": "かあちゃん", "discord_user_id": 999}, ensure_ascii=False), encoding="utf-8"
    )
    # 親: 兼務くんと同ID(id=555) ← 親にも登録され is_parent=True になる
    (tmp / "settings" / "users" / "parents" / "kenmu_parent.json").write_text(
        json.dumps({"name": "兼務おや", "discord_user_id": 555}, ensure_ascii=False), encoding="utf-8"
    )
    config.SETTINGS_DIR = tmp / "settings"
    config.USERS_DIR = config.SETTINGS_DIR / "users"
    config.PARENTS_DIR = config.USERS_DIR / "parents"


def _run() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _setup(tmp)

        # 純粋な親(999): 子ディレクトリには居ない → find_child_user_by_discord_id は None
        _check(
            "pure_parent_not_a_child",
            config.find_child_user_by_discord_id(999) is None,
            "pure parent must NOT resolve as child (else block is bypassed and 越境 revives)",
        )
        # だが find_user_by_discord_id(親優先)は親設定を返す = r9のバグ源。子判定に使ってはいけない
        _check(
            "find_user_returns_parent_for_pure_parent",
            (config.find_user_by_discord_id(999) or {}).get("name") == "かあちゃん",
            "find_user_by_discord_id returns parent (why it must NOT be used for child check)",
        )
        # 兼務アカウント(555): 子ディレクトリに実在 → 通すべき
        _check(
            "dual_role_is_a_child",
            (config.find_child_user_by_discord_id(555) or {}).get("name") == "兼務くん",
            "dual-role account IS a registered child, must pass to conversation",
        )
        # 子本人(111): 子ディレクトリに実在
        _check(
            "real_child_is_a_child",
            (config.find_child_user_by_discord_id(111) or {}).get("name") == "たろう",
            "",
        )

        # ブロック判定の再現(bot.py の実装と同一): is_parent かつ proxy無し かつ
        # 「発話者の子本人 == いま対象の user_conf の子」でない → ブロック。
        # 本人性は discord_user_id(int) で照合する(name 文字列でなく id が正本)。
        # target_conf は _find_channel_child_user_conf が解決したチャンネルの子conf。
        def _would_block(discord_id: int, is_parent: bool, has_proxy: bool, target_conf: dict) -> bool:
            author_child = config.find_child_user_by_discord_id(discord_id)
            a_id = config._safe_int((author_child or {}).get("discord_user_id"), -1)
            t_id = config._safe_int((target_conf or {}).get("discord_user_id"), -2)
            author_is_this_channel_child = author_child is not None and a_id == t_id
            return is_parent and (not has_proxy) and (not author_is_this_channel_child)

        tarou = {"name": "たろう", "discord_user_id": 111}
        kenmu = {"name": "兼務くん", "discord_user_id": 555}
        # 純粋な親の自然文(proxy無し、たろうチャンネル) → ブロックされる(越境阻止)
        _check("pure_parent_blocked", _would_block(999, is_parent=True, has_proxy=False, target_conf=tarou) is True)
        # 兼務アカウント(555)が【自分=兼務くんのチャンネル】で発話 → id一致 → 通す
        _check("dual_role_own_channel_not_blocked", _would_block(555, is_parent=True, has_proxy=False, target_conf=kenmu) is False)
        # 【重要・r11】兼務アカウント(555)が【別の子=たろうのチャンネル】で発話 → id不一致 → ブロック(越境阻止)
        _check("dual_role_other_channel_blocked", _would_block(555, is_parent=True, has_proxy=False, target_conf=tarou) is True)
        # 【r12・name衝突耐性】対象conf が「たろうと同名だが別id(222)」でも、id で照合するので兼務くん(555)とは不一致 → ブロック
        tarou_lookalike = {"name": "たろう", "discord_user_id": 222}
        _check("same_name_different_id_blocked", _would_block(555, is_parent=True, has_proxy=False, target_conf=tarou_lookalike) is True)
        # 親でも代理指定あり → ブロックされない(代理経路)
        _check("parent_with_proxy_not_blocked", _would_block(999, is_parent=True, has_proxy=True, target_conf=tarou) is False)
        # 子本人(is_parent=False) → ブロックされない
        _check("child_not_blocked", _would_block(111, is_parent=False, has_proxy=False, target_conf=tarou) is False)

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))


if __name__ == "__main__":
    _run()
