from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from aicrm_next.platform.platform_foundation.job_runs import FinishJobRunRequest, StartJobRunRequest, build_job_run_ledger_port

from aicrm_next.crm.identity_contact.resolver import resolve_external_userid_with_dbapi
from aicrm_next.crm.identity_contact.oneid_repository import (
    PostgresOneIDService,
    WECOM_EXTERNAL_IDENTITY,
    WECOM_PROVIDER,
)
from aicrm_next.platform.shared.postgres_connection import PostgresConnection
from aicrm_next.crm.identity_contact.resolution_queue_port import (
    EnqueueIdentityResolutionRequest,
    build_identity_resolution_queue_port,
)
from aicrm_next.platform.shared.repository_provider import assert_repository_allowed
from aicrm_next.platform.shared.runtime import production_data_ready, raw_database_url
from aicrm_next.platform.shared.runtime_settings import runtime_setting
from aicrm_next.platform.shared.typing import JsonDict


class MessageArchiveRepository(Protocol):
    def list_messages(
        self,
        external_userid: str,
        *,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]: ...

    def search_messages(
        self,
        *,
        external_userid: str,
        keyword: str,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]: ...

    def list_external_chat_records(
        self,
        *,
        external_userid: str,
        chat_scene: str,
        start_time: str,
        with_userid: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[JsonDict], int]: ...


class ArchiveSyncRepository(Protocol):
    def create_sync_run(self, *, start_time: str, end_time: str, owner_userid: str, cursor: str) -> int: ...

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        fetched_count: int,
        inserted_count: int,
        raw_response: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None: ...

    def get_archive_last_seq(self) -> int: ...

    def insert_messages_and_advance_seq(self, messages: list[JsonDict], *, last_seq: int) -> int: ...


class FixtureMessageArchiveRepository:
    def __init__(self) -> None:
        self._messages: list[JsonDict] = [
            {
                "seq": 1,
                "msgid": "msg-ext-private-001",
                "chat_type": "private",
                "external_userid": "wx_ext_001",
                "owner_userid": "HuangYouCan",
                "sender": "wx_ext_001",
                "from": "wx_ext_001",
                "receiver": "HuangYouCan",
                "tolist": ["HuangYouCan"],
                "roomid": "",
                "chat_id": "",
                "group_name": "",
                "msgtype": "text",
                "content": "我刚买了 9.9，想知道第一步怎么做",
                "send_time": "2026-06-02T08:06:00+00:00",
            },
            {
                "seq": 2,
                "msgid": "msg-ext-private-002",
                "chat_type": "private",
                "external_userid": "wx_ext_001",
                "owner_userid": "HuangYouCan",
                "sender": "HuangYouCan",
                "from": "HuangYouCan",
                "receiver": "wx_ext_001",
                "tolist": ["wx_ext_001"],
                "roomid": "",
                "chat_id": "",
                "group_name": "",
                "msgtype": "text",
                "content": "先把你的目标和卡点发我",
                "send_time": "2026-06-02T08:08:00+00:00",
            },
            {
                "seq": 3,
                "msgid": "msg-ext-group-001",
                "chat_type": "group",
                "external_userid": "wx_ext_001",
                "owner_userid": "HuangYouCan",
                "sender": "HuangYouCan",
                "from": "HuangYouCan",
                "receiver": "",
                "tolist": [],
                "roomid": "wr_hxc_group_001",
                "chat_id": "wr_hxc_group_001",
                "group_name": "黄小璨体验群",
                "msgtype": "text",
                "content": "今晚 8 点有首月体验答疑",
                "send_time": "2026-06-02T08:10:00+00:00",
            },
            {
                "seq": 11,
                "msgid": "msg-001",
                "chat_type": "private",
                "external_userid": "wm_ext_001",
                "owner_userid": "sales_01",
                "sender": "wm_ext_001",
                "from": "wm_ext_001",
                "tolist": ["sales_01"],
                "roomid": "",
                "chat_id": "",
                "group_name": "",
                "msgtype": "text",
                "content": "我想了解真实落地案例",
                "send_time": "2026-03-15 09:30:00",
            },
            {
                "seq": 12,
                "msgid": "msg-002",
                "chat_type": "private",
                "external_userid": "wm_ext_001",
                "owner_userid": "sales_01",
                "sender": "sales_01",
                "from": "sales_01",
                "tolist": ["wm_ext_001"],
                "roomid": "",
                "chat_id": "",
                "group_name": "",
                "msgtype": "text",
                "content": "这里是方案和报名说明",
                "send_time": "2026-03-15 09:35:00",
            },
            {
                "seq": 13,
                "msgid": "msg-003",
                "chat_type": "group",
                "external_userid": "wm_ext_001",
                "owner_userid": "sales_01",
                "sender": "sales_01",
                "from": "sales_01",
                "tolist": [],
                "roomid": "wr_group_001",
                "chat_id": "wr_group_001",
                "group_name": "测试群",
                "msgtype": "text",
                "content": "群内跟进记录",
                "send_time": "2026-03-15 09:40:00",
            },
        ]

    def list_messages(
        self,
        external_userid: str,
        *,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]:
        rows = [deepcopy(row) for row in self._messages if row.get("external_userid") == external_userid]
        if chat_type:
            rows = [row for row in rows if row.get("chat_type") == chat_type]
        return _page(rows, limit=limit, offset=offset)

    def search_messages(
        self,
        *,
        external_userid: str,
        keyword: str,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]:
        rows = [row for row in self.list_messages(external_userid, chat_type=chat_type, limit=None, offset=0) if keyword in str(row.get("content") or "")]
        return _page(rows, limit=limit, offset=offset)

    def list_external_chat_records(
        self,
        *,
        external_userid: str,
        chat_scene: str,
        start_time: str,
        with_userid: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[JsonDict], int]:
        rows = [
            deepcopy(row)
            for row in self._messages
            if row.get("external_userid") == external_userid and _message_scene(row) == chat_scene and _message_after_start(row, start_time)
        ]
        if chat_scene == "private" and with_userid:
            rows = [row for row in rows if _message_matches_with_user(row, with_userid)]
        rows.sort(key=lambda row: (_timestamp_key(row.get("send_time")), int(row.get("seq") or 0)))
        page = _page(rows, limit=limit, offset=offset)
        return [_project_external_chat_record(row) for row in page], len(rows)


class PostgresMessageArchiveReadRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = _psycopg_url(str(database_url or raw_database_url()).strip())

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._database_url, row_factory=dict_row)

    def list_messages(
        self,
        external_userid: str,
        *,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]:
        normalized = str(chat_type or "").strip().lower()
        with self._connect() as conn:
            unionid = resolve_external_userid_with_dbapi(conn, str(external_userid or "").strip())
            if not unionid:
                return []
            clauses = ["message.unionid = %s"]
            params: list[Any] = [unionid]
            if normalized:
                scene_values = ("private", "single") if _normalize_stored_chat_scene(normalized) == "private" else ("group",)
                clauses.append("message.chat_type = ANY(%s)")
                params.append(list(scene_values))
            where_sql = " AND ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT message.id, message.msgid, message.chat_type, message.unionid,
                       COALESCE(identity.primary_external_userid, '') AS external_userid,
                       message.owner_userid, message.sender, message.receiver,
                       message.msgtype, message.content, message.send_time, message.raw_payload, message.created_at
                FROM archived_messages message
                LEFT JOIN crm_user_identity identity ON identity.unionid = message.unionid
                WHERE {where_sql}
                ORDER BY message.send_time DESC, message.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [int(limit or 20), int(offset or 0)]),
            ).fetchall()
        return [_project_external_chat_record(dict(row)) for row in rows]

    def search_messages(
        self,
        *,
        external_userid: str,
        keyword: str,
        chat_type: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JsonDict]:
        normalized = str(chat_type or "").strip().lower()
        needle = str(keyword or "")
        if not needle:
            return []
        with self._connect() as conn:
            unionid = resolve_external_userid_with_dbapi(conn, str(external_userid or "").strip())
            if not unionid:
                return []
            clauses = [
                "message.unionid = %s",
                "message.unionid <> ''",
                "message.content <> ''",
                "message.content LIKE %s ESCAPE '\\'",
            ]
            params: list[Any] = [unionid, _literal_like_pattern(needle)]
            if normalized:
                scene_values = ("private", "single") if _normalize_stored_chat_scene(normalized) == "private" else ("group",)
                clauses.append("message.chat_type = ANY(%s)")
                params.append(list(scene_values))
            where_sql = " AND ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT message.id, message.msgid, message.chat_type, message.unionid,
                       COALESCE(identity.primary_external_userid, '') AS external_userid,
                       message.owner_userid, message.sender, message.receiver,
                       message.msgtype, message.content, message.send_time, message.raw_payload, message.created_at
                FROM archived_messages message
                LEFT JOIN crm_user_identity identity ON identity.unionid = message.unionid
                WHERE {where_sql}
                ORDER BY message.send_time DESC, message.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [int(limit or 20), max(0, int(offset or 0))]),
            ).fetchall()
        return [_project_external_chat_record(dict(row)) for row in rows]

    def list_external_chat_records(
        self,
        *,
        external_userid: str,
        chat_scene: str,
        start_time: str,
        with_userid: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[JsonDict], int]:
        scene_values = ("private", "single") if chat_scene == "private" else ("group",)
        with self._connect() as conn:
            unionid = resolve_external_userid_with_dbapi(conn, str(external_userid or "").strip())
            if not unionid:
                return [], 0
            clauses = ["message.unionid = %s", "message.chat_type = ANY(%s)", "message.send_time >= %s"]
            params: list[Any] = [unionid, list(scene_values), start_time]
            if chat_scene == "private" and str(with_userid or "").strip():
                clauses.append("(message.owner_userid = %s OR message.sender = %s OR message.receiver = %s)")
                peer = str(with_userid or "").strip()
                params.extend([peer, peer, peer])
            where_sql = " AND ".join(clauses)
            total = int(
                (
                    conn.execute(
                        f"SELECT COUNT(*) AS total FROM archived_messages message WHERE {where_sql}",
                        tuple(params),
                    ).fetchone()
                    or {}
                ).get("total")
                or 0
            )
            rows = conn.execute(
                f"""
                SELECT message.id, message.msgid, message.chat_type, message.unionid,
                       COALESCE(identity.primary_external_userid, '') AS external_userid,
                       message.owner_userid, message.sender, message.receiver,
                       message.msgtype, message.content, message.send_time, message.raw_payload, message.created_at
                FROM archived_messages message
                LEFT JOIN crm_user_identity identity ON identity.unionid = message.unionid
                WHERE {where_sql}
                ORDER BY message.send_time ASC, message.id ASC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [int(limit), int(offset)]),
            ).fetchall()
        return [_project_external_chat_record(dict(row)) for row in rows], total


class PostgresArchiveSyncRepository:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        source_change_recorder: Callable[[Any, int, int, str, list[int]], dict[str, Any]] | None = None,
    ) -> None:
        self._database_url = _psycopg_url(str(database_url or raw_database_url()).strip())
        self._source_change_recorder = source_change_recorder

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._database_url, row_factory=dict_row)

    def create_sync_run(self, *, start_time: str, end_time: str, owner_userid: str, cursor: str) -> int:
        with self._connect() as conn:
            run_id = build_job_run_ledger_port().start_dbapi(
                conn,
                request=StartJobRunRequest(
                    start_time=start_time,
                    end_time=end_time,
                    owner_userid=owner_userid,
                    cursor=cursor,
                ),
            )
            conn.commit()
        return run_id

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        fetched_count: int,
        inserted_count: int,
        raw_response: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        with self._connect() as conn:
            build_job_run_ledger_port().finish_dbapi(
                conn,
                run_id,
                request=FinishJobRunRequest(
                    status=status,
                    fetched_count=fetched_count,
                    inserted_count=inserted_count,
                    raw_response=raw_response,
                    error_message=error_message,
                ),
            )
            conn.commit()

    def get_archive_last_seq(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT last_seq FROM archive_sync_state WHERE state_key = 'global'").fetchone()
        return int((row or {}).get("last_seq") or 0)

    def insert_messages_and_advance_seq(self, messages: list[JsonDict], *, last_seq: int) -> int:
        inserted = 0
        inserted_row_ids: list[int] = []
        with self._connect() as conn:
            try:
                for message in messages:
                    external_userid = str(message.get("external_userid") or "").strip()
                    customer_id = 0
                    customer_identity_id = 0
                    if external_userid:
                        identity = PostgresOneIDService().ensure_verified_identity_with_db(
                            PostgresConnection(conn),
                            provider=WECOM_PROVIDER,
                            identity_type=WECOM_EXTERNAL_IDENTITY,
                            scope_key=runtime_setting("WECOM_CORP_ID", "").strip(),
                            normalized_value=external_userid,
                            source_type="wecom_message_archive",
                            source_event_id=str(message.get("msgid") or "").strip(),
                        )
                        customer_id = identity.customer_id
                        customer_identity_id = identity.identity_id
                    unionid = str(message.get("unionid") or "").strip()
                    if not unionid:
                        unionid = resolve_external_userid_with_dbapi(conn, external_userid)
                    row = conn.execute(
                        """
                        INSERT INTO archived_messages (
                            seq, msgid, chat_type, customer_id, customer_identity_id,
                            external_userid, unionid, owner_userid, sender, receiver,
                            msgtype, content, send_time, raw_payload
                        )
                        VALUES (%s, %s, %s, NULLIF(%s, 0), NULLIF(%s, 0), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (msgid) DO NOTHING
                        RETURNING id
                        """,
                        (
                            int(message.get("seq") or 0),
                            str(message.get("msgid") or ""),
                            str(message.get("chat_type") or "private"),
                            customer_id,
                            customer_identity_id,
                            external_userid,
                            unionid,
                            str(message.get("owner_userid") or ""),
                            str(message.get("sender") or ""),
                            str(message.get("receiver") or ""),
                            str(message.get("msgtype") or "text"),
                            str(message.get("content") or ""),
                            str(message.get("send_time") or ""),
                            str(message.get("raw_payload") or ""),
                        ),
                    ).fetchone()
                    if row:
                        inserted += 1
                        row_id = int(row.get("id") or 0)
                        if row_id <= 0:
                            raise RuntimeError("archive_inserted_row_id_required")
                        inserted_row_ids.append(row_id)
                conn.execute(
                    """
                    INSERT INTO archive_sync_state (state_key, last_seq, updated_at)
                    VALUES ('global', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (state_key) DO UPDATE SET
                        last_seq = GREATEST(archive_sync_state.last_seq, excluded.last_seq),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (int(last_seq),),
                )
                if inserted:
                    if self._source_change_recorder is None:
                        raise RuntimeError("archive_source_change_recorder_required")
                    self._source_change_recorder(
                        conn,
                        int(inserted),
                        int(last_seq),
                        _archive_source_batch_key(inserted_row_ids),
                        inserted_row_ids,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return inserted


def build_message_archive_repository() -> MessageArchiveRepository:
    if production_data_ready():
        return assert_repository_allowed(PostgresMessageArchiveReadRepository(), capability_owner="message_archive")
    return assert_repository_allowed(FixtureMessageArchiveRepository(), capability_owner="message_archive")


def build_archive_sync_repository(
    *,
    source_change_recorder: Callable[[Any, int, int, str, list[int]], dict[str, Any]] | None = None,
) -> ArchiveSyncRepository:
    if not production_data_ready():
        raise RuntimeError("archive sync requires PostgreSQL production data mode")
    return assert_repository_allowed(
        PostgresArchiveSyncRepository(source_change_recorder=source_change_recorder),
        capability_owner="message_archive",
    )


def _archive_source_batch_key(inserted_row_ids: list[int]) -> str:
    normalized = sorted(int(row_id) for row_id in inserted_row_ids if int(row_id) > 0)
    if not normalized:
        raise RuntimeError("archive_source_batch_key_required")
    material = ",".join(str(row_id) for row_id in normalized)
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def read_archive_app_setting(key: str) -> str:
    return runtime_setting(key, "")


def _page(rows: list[JsonDict], *, limit: int | None, offset: int) -> list[JsonDict]:
    if limit is None:
        return rows[offset:] if offset else rows
    return rows[offset : offset + limit]


def _literal_like_pattern(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _psycopg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _enqueue_archive_identity_resolution(conn, message: JsonDict) -> None:
    external_userid = str(message.get("external_userid") or "").strip()
    source_key = external_userid
    if not external_userid:
        return
    build_identity_resolution_queue_port().enqueue_dbapi(
        conn,
        EnqueueIdentityResolutionRequest(
            source_type="archived_messages",
            source_key=source_key,
            reason="missing_unionid",
            source_route="message_archive.identity_resolution.enqueue",
            external_userid=external_userid,
            payload_json={"message": message},
        ),
    )


def _normalize_stored_chat_scene(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "private", "single"}:
        return "private"
    return "group" if text == "group" else text


def _message_scene(row: JsonDict) -> str:
    return _normalize_stored_chat_scene(str(row.get("chat_type") or ""))


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _timestamp_key(value: Any) -> datetime:
    return _parse_timestamp(value) or datetime(1970, 1, 1, tzinfo=timezone.utc)


def _message_after_start(row: JsonDict, start_time: str) -> bool:
    send_time = _parse_timestamp(row.get("send_time"))
    start = _parse_timestamp(start_time)
    if not start:
        return True
    return bool(send_time and send_time >= start)


def _message_matches_with_user(row: JsonDict, with_userid: str) -> bool:
    peer = str(with_userid or "").strip()
    if not peer:
        return True
    return peer in {
        str(row.get("owner_userid") or "").strip(),
        str(row.get("sender") or "").strip(),
        str(row.get("from") or "").strip(),
        str(row.get("receiver") or "").strip(),
        *[str(item or "").strip() for item in list(row.get("tolist") or [])],
    }


def _project_external_chat_record(row: JsonDict) -> JsonDict:
    raw_payload = _json_payload(row.get("raw_payload"))
    chat_id = str(row.get("chat_id") or row.get("roomid") or raw_payload.get("roomid") or raw_payload.get("chat_id") or "").strip()
    return {
        "msgid": str(row.get("msgid") or "").strip(),
        "chat_scene": _message_scene(row),
        "chat_type": str(row.get("chat_type") or "").strip(),
        "unionid": str(row.get("unionid") or "").strip(),
        "external_userid": str(row.get("external_userid") or "").strip(),
        "with_userid": str(row.get("owner_userid") or "").strip(),
        "sender": str(row.get("sender") or row.get("from") or "").strip(),
        "receiver": str(row.get("receiver") or "").strip(),
        "chat_id": chat_id,
        "roomid": str(row.get("roomid") or raw_payload.get("roomid") or "").strip(),
        "group_name": str(row.get("group_name") or raw_payload.get("group_name") or "").strip(),
        "msgtype": str(row.get("msgtype") or "text").strip() or "text",
        "content": str(row.get("content") or "").strip(),
        "send_time": str(row.get("send_time") or "").strip(),
        "source_id": str(row.get("id") or row.get("seq") or "").strip(),
    }
