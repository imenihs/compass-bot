import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def now_jst_iso() -> str:
    return datetime.now(JST).isoformat()


def _read_jsonl(path: Path) -> list[dict]:
    """JSONL ファイルを dict のリストとして読む。無ければ空。壊れた行はスキップ。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    rows.append(rec)
            except json.JSONDecodeError:
                continue
    return rows


def count_recent_allowance_requests(log_dir: Path, user_name: str, days: int = 30) -> int:
    """直近 days 日で査定(assessed)された回数を数える。Gemini とは無関係のログ集計。

    元は gemini_service.py にあったが、Gemini 完全削除に伴い storage へ移設。events ログの
    assessed フラグ付きレコードを日数窓で数えるだけで、AI 呼び出しは一切しない。
    """
    path = log_dir / f"{user_name}_events.jsonl"
    rows = _read_jsonl(path)
    now = datetime.now(JST)
    count = 0
    for r in rows:
        ts = r.get("ts")
        assessed = r.get("assessed")
        if not ts or not assessed:
            continue
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if (now - dt).days <= days:
            count += 1
    return count


# ----------------------------------------------------------------------
# 会話ログ（{name}_conversation.jsonl）
# events.jsonl とは別系統。全ターンで書かれる最頻の追記経路であり、
# 保持は行数上限とアーカイブの保持日数（社長決定=90日）で切り詰める。
# ----------------------------------------------------------------------

# アーカイブファイル名は「<元名>.YYYYMMDD-HHMMSS-ffffff.archive.jsonl」に固定する。
# 退避時刻をマイクロ秒まで名前へ埋め、保持日数超過の判定と改名時の一括移送（第5段）で機械的に拾えるようにする。
_ARCHIVE_SUFFIX = ".archive.jsonl"
# マイクロ秒（%f）まで含めることで同一秒の連続退避でも名前が一意になり、連番付与を廃する。
# 連番があると退避時刻の直後に "-1" 等が挟まり _ARCHIVE_RE が拾えず、保持日数削除から永久に漏れるため。
_ARCHIVE_TS_FMT = "%Y%m%d-%H%M%S-%f"
# アーカイブ名から退避時刻を取り出す正規表現。元ファイル名は任意、退避時刻＋固定サフィックスに一致させる。
# 退避時刻は「日付8桁-時分秒6桁-マイクロ秒6桁」。旧形式（マイクロ秒無し・末尾連番付き）も安全側で拾えるよう、
# マイクロ秒部と連番部の双方を任意（省略可）にして後方互換で解析する。読めないアーカイブは削除対象から外れるため、拾える形にしておく。
_ARCHIVE_RE = re.compile(
    r"\.(?P<ts>\d{8}-\d{6}(?:-\d{6})?)(?:-\d+)?" + re.escape(_ARCHIVE_SUFFIX) + r"$"
)


def append_conversation(path: Path, record: dict) -> None:
    """会話ログへ1行追記する。原子性もロックも持たない素の追記である。

    append_jsonl と同じく単純追記に徹する。会話ログの記録は応答の出口（reply.py）が
    session.py のロック外で呼ぶ（rotate と送信を臨界区間の外で回す設計のため）。したがって
    本関数はターン間の順序を保証しない。同一児童の2ターンが並行すると入力→応答の並びが
    乱れうるが、1回の write で行自体は壊れず、同一児童の同時ターンは実運用では稀である。
    厳密な順序が要る場合は呼び出し側で直列化すること。
    ts が無い record には JST の現在時刻を補う。各行に発話時刻を残し、
    将来プロンプトへ渡す会話履歴を時系列・種別でフィルタできるようにするため。

    なお rotate の保持日数判定が根拠にするのはこの record.ts ではなく、
    アーカイブ『ファイル名』へ埋めた退避時刻（_archive_ts）である。両者を混同しない。

    Args:
        path: 書き込み先（例: {log_dir}/{name}_conversation.jsonl）。
        record: 追記する1レコード。ts が無ければ本関数が補う。
    """
    # 各行に発話時刻を残すため、ts が無いレコードにのみ現在時刻を補う
    if "ts" not in record:
        record = {"ts": now_jst_iso(), **record}
    path.parent.mkdir(parents=True, exist_ok=True)
    # 追記自体は排他を持たない。順序保証は無い（同一児童の同時ターンは稀）
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _archive_ts(archive_path: Path) -> datetime | None:
    """アーカイブファイル名に埋めた退避時刻を datetime（JST）で返す。読めなければ None。

    現行名（マイクロ秒付き `YYYYMMDD-HHMMSS-ffffff`）と、旧名（マイクロ秒無し
    `YYYYMMDD-HHMMSS`・末尾連番付きを含む）の双方を解析する。マイクロ秒の有無で
    パースフォーマットを切り替え、どちらでも退避時刻を返せるようにする。
    保持日数削除は本値を根拠にするため、拾えないと当該アーカイブが永久に残る。
    """
    m = _ARCHIVE_RE.search(archive_path.name)
    if not m:
        return None
    ts_text = m.group("ts")
    # マイクロ秒部の有無でフォーマットを選ぶ。旧名（マイクロ秒無し）も後方互換で解析する
    fmt = _ARCHIVE_TS_FMT if ts_text.count("-") >= 2 else "%Y%m%d-%H%M%S"
    try:
        dt = datetime.strptime(ts_text, fmt)
    except ValueError:
        return None
    # ファイル名の退避時刻は JST で書いているため JST として aware 化する
    return dt.replace(tzinfo=JST)


def rotate(path: Path, *, max_lines: int, retention_days: int) -> None:
    """会話ログを行数上限と保持日数で切り詰める。臨界区間の外から呼ぶ前提。

    2段構えで運用する。
      1) 行数切り詰め: live ファイルが max_lines を超えたら、超過した古い行を
         タイムスタンプ付きアーカイブへ退避し、新しい max_lines 行だけを live に残す。
         退避と live の書き換えはともに tmp+replace で行う。
      2) 保持日数超過アーカイブの削除: 退避時刻が retention_days より古いアーカイブを消す。
    ローテーション中に届いたターンは同一ループ上で直列化される前提であり、
    本関数自体はロックを持たない。臨界区間の内側からは呼ばない。

    Args:
        path: live 会話ログのパス（例: {log_dir}/{name}_conversation.jsonl）。
        max_lines: live ファイルに残す最大行数。0以下なら行数切り詰めは行わない。
        retention_days: アーカイブの保持日数。0以下なら日数削除は行わない。
    """
    # --- 1) 行数による切り詰め ---
    if max_lines and max_lines > 0 and path.exists():
        # 全行を読む。改行のみの空行は捨て、有効行だけを対象にする
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        # 上限超過分（先頭側の古い行）だけをアーカイブへ回す
        if len(lines) > max_lines:
            overflow = lines[:-max_lines]  # 退避する古い行
            keep = lines[-max_lines:]      # live に残す新しい行
            # 退避時刻（マイクロ秒まで）をファイル名へ埋めたアーカイブを作る。マイクロ秒により通常は一意になる
            stamp = datetime.now(JST).strftime(_ARCHIVE_TS_FMT)
            archive_path = path.with_name(f"{path.name}.{stamp}{_ARCHIVE_SUFFIX}")
            seq = 1
            while archive_path.exists():
                # 同一マイクロ秒での衝突という極めて稀な事態への保険。末尾連番は退避時刻グループの後に置く。
                # _ARCHIVE_RE は退避時刻を先に確定させ末尾 "-\d+" を任意で許容するため、連番付きでも _archive_ts が拾える
                archive_path = path.with_name(f"{path.name}.{stamp}-{seq}{_ARCHIVE_SUFFIX}")
                seq += 1
            # 退避先を tmp+replace で原子的に作る（既存アーカイブがあれば追記ではなく別名になっている）
            arc_tmp = archive_path.with_suffix(archive_path.suffix + ".tmp")
            with open(arc_tmp, "w", encoding="utf-8") as f:
                for ln in overflow:
                    f.write(ln + "\n")
            arc_tmp.replace(archive_path)
            # live を新しい行だけで tmp+replace 書き換えする。退避成功後にのみ live を縮める
            live_tmp = path.with_suffix(path.suffix + ".tmp")
            with open(live_tmp, "w", encoding="utf-8") as f:
                for ln in keep:
                    f.write(ln + "\n")
            live_tmp.replace(path)

    # --- 2) 保持日数を超えたアーカイブの削除 ---
    if retention_days and retention_days > 0:
        cutoff = datetime.now(JST) - timedelta(days=retention_days)
        # 同ディレクトリの、当該 live に対応するアーカイブだけを走査する
        prefix = path.name + "."
        for arc in path.parent.glob(f"{path.name}.*{_ARCHIVE_SUFFIX}"):
            # 念のため対象 live のアーカイブに限定する（他ユーザーのファイルを消さない）
            if not arc.name.startswith(prefix):
                continue
            ts = _archive_ts(arc)
            # 退避時刻が読めないアーカイブは安全側で残す（誤削除を避ける）
            if ts is None:
                continue
            if ts < cutoff:
                arc.unlink(missing_ok=True)
