# ruff: noqa: F401
from __future__ import annotations

from .repo_support import (
    Any,
    CHOICE_QUESTION_TYPES,
    Callable,
    ContractError,
    Protocol,
    QuestionnaireRepository,
    RepositoryProviderError,
    ResolvePersonIdentityRequest,
    _answer_snapshots,
    _answer_value_list,
    _answers_from_snapshots,
    _as_bool,
    _external_answer_projection,
    _external_push_log_threads,
    _external_push_payload,
    _external_submission_projection,
    _identity_lookup_values,
    _initial_questionnaires,
    _json_dict,
    _json_dumps,
    _json_list,
    _json_payload,
    _jsonb,
    _matches_external_submission_filters,
    _mobile_answer,
    _normalized_external_push_log,
    _now,
    _optional_float,
    _optional_int,
    _parse_comparable_timestamp,
    _psycopg_url,
    _questionnaire_payload,
    _slugify_questionnaire,
    _text,
    _text_answer,
    _timestamp,
    assert_repository_allowed,
    datetime,
    deepcopy,
    enqueue_questionnaire_identity_resolution,
    enqueue_transactional_internal_event_outbox,
    json,
    normalize_completion_target_for_storage,
    production_data_ready,
    raw_database_url,
    re,
    resolve_identity_with_dbapi,
    resolved_unionid,
    runtime_setting,
    selected_choice_options,
    timezone,
    uuid4,
)
from .repo_memory import InMemoryQuestionnaireRepository


class PostgresQuestionnaireReadRepository:
    source_status = "next_read_model"
    read_model_status = "primary"

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = _psycopg_url(str(database_url or raw_database_url()).strip())
        if not self._database_url:
            raise RepositoryProviderError("questionnaire production read repository unavailable: DATABASE_URL is required")

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:  # pragma: no cover - dependency failure varies by runtime
            raise RepositoryProviderError("psycopg is required for questionnaire production read repository") from exc
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def _questionnaire_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        enabled = not bool(row.get("is_disabled"))
        external_push_config = {
            "enabled": bool(row.get("external_push_enabled")),
            "webhook_url": _text(row.get("external_push_url")),
            "type": _text(row.get("external_push_type")),
            "expires_at_ts": row.get("external_push_expires_at_ts"),
            "day": row.get("external_push_day"),
            "frequency": row.get("external_push_frequency"),
            "remark": _text(row.get("external_push_remark")),
            "custom_params": _json_list(row.get("external_push_custom_params")),
        }
        return {
            "id": int(row["id"]),
            "slug": _text(row.get("slug")),
            "name": _text(row.get("name")),
            "title": _text(row.get("title") or row.get("name")),
            "description": _text(row.get("description")),
            "enabled": enabled,
            "is_disabled": not enabled,
            "status": "disabled" if not enabled else "published",
            "version": int(row.get("version") or 1),
            "redirect_url": _text(row.get("redirect_url")),
            "lead_channel_id": int(row.get("lead_channel_id") or 0) or None,
            "lead_qr_title": _text(row.get("lead_qr_title")),
            "lead_qr_subtitle": _text(row.get("lead_qr_subtitle")),
            "completion_target_json": _json_dict(row.get("completion_target_json")),
            "answer_display_mode": _text(row.get("answer_display_mode") or "all_in_one"),
            "assessment_enabled": bool(row.get("assessment_enabled")),
            "assessment_config": _json_dict(row.get("assessment_config")),
            "result_config": _json_dict(row.get("assessment_config")),
            "external_push_config": external_push_config,
            "external_push_enabled": external_push_config["enabled"],
            "external_push_url": external_push_config["webhook_url"],
            "external_push_type": external_push_config["type"],
            "external_push_expires_at_ts": external_push_config["expires_at_ts"],
            "external_push_day": external_push_config["day"],
            "external_push_frequency": external_push_config["frequency"],
            "external_push_remark": external_push_config["remark"],
            "external_push_custom_params": external_push_config["custom_params"],
            "created_at": _timestamp(row.get("created_at")),
            "updated_at": _timestamp(row.get("updated_at")),
            "question_count": int(row.get("question_count") or 0),
            "submission_count": int(row.get("submission_count") or 0),
            "last_submitted_at": _timestamp(row.get("last_submitted_at")),
            "questions": [],
            "rules": [],
            "score_rules": [],
            "submissions_summary": {},
            "submissions": [],
        }

    def _base_select(self) -> str:
        return """
            SELECT
                q.*,
                1 AS version,
                COALESCE(question_counts.question_count, 0) AS question_count,
                COALESCE(submission_counts.submission_count, 0) AS submission_count,
                submission_counts.last_submitted_at AS last_submitted_at
            FROM questionnaires q
            LEFT JOIN (
                SELECT questionnaire_id, COUNT(*) AS question_count
                FROM questionnaire_questions
                GROUP BY questionnaire_id
            ) question_counts ON question_counts.questionnaire_id = q.id
            LEFT JOIN (
                SELECT questionnaire_id, COUNT(*) AS submission_count, MAX(submitted_at) AS last_submitted_at
                FROM questionnaire_submissions
                GROUP BY questionnaire_id
            ) submission_counts ON submission_counts.questionnaire_id = q.id
        """

    def _paged_select(self) -> str:
        return """
            WITH questionnaire_page AS (
                SELECT *
                FROM questionnaires
                ORDER BY updated_at DESC, id DESC
                LIMIT %s OFFSET %s
            )
            SELECT
                q.*,
                1 AS version,
                COALESCE(question_counts.question_count, 0) AS question_count,
                COALESCE(submission_counts.submission_count, 0) AS submission_count,
                submission_counts.last_submitted_at AS last_submitted_at
            FROM questionnaire_page q
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS question_count
                FROM questionnaire_questions question
                WHERE question.questionnaire_id = q.id
            ) question_counts ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS submission_count, MAX(submitted_at) AS last_submitted_at
                FROM questionnaire_submissions submission
                WHERE submission.questionnaire_id = q.id
            ) submission_counts ON TRUE
            ORDER BY q.updated_at DESC, q.id DESC
        """

    def list_questionnaires(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        with self._connect() as conn:
            total = int((conn.execute("SELECT COUNT(*) AS total FROM questionnaires").fetchone() or {}).get("total") or 0)
            rows = conn.execute(
                self._paged_select(),
                (safe_limit, safe_offset),
            ).fetchall()
        return [self._questionnaire_from_row(dict(row)) for row in rows], total

    def get_questionnaire(self, questionnaire_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(self._base_select() + " WHERE q.id = %s", (int(questionnaire_id),)).fetchone()
        if not row:
            return None
        item = self._questionnaire_from_row(dict(row))
        item["questions"] = self.list_questions(questionnaire_id) or []
        item["rules"] = self._list_score_rules(questionnaire_id)
        item["score_rules"] = deepcopy(item["rules"])
        item["submissions_summary"] = self.get_results_summary(questionnaire_id) or {}
        submissions = self.list_submissions(questionnaire_id, limit=10, offset=0)
        item["submissions"] = submissions[0] if submissions else []
        return item

    def get_questionnaire_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(self._base_select() + " WHERE q.slug = %s", (str(slug or "").strip(),)).fetchone()
        if not row:
            return None
        return self.get_questionnaire(int(row["id"]))

    def list_questions(self, questionnaire_id: int) -> list[dict[str, Any]] | None:
        if not self._exists(questionnaire_id):
            return None
        with self._connect() as conn:
            question_rows = conn.execute(
                """
                SELECT *
                FROM questionnaire_questions
                WHERE questionnaire_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (int(questionnaire_id),),
            ).fetchall()
            option_rows = conn.execute(
                """
                SELECT qo.*
                FROM questionnaire_options qo
                JOIN questionnaire_questions qq ON qq.id = qo.question_id
                WHERE qq.questionnaire_id = %s
                ORDER BY qo.sort_order ASC, qo.id ASC
                """,
                (int(questionnaire_id),),
            ).fetchall()
        options_by_question: dict[int, list[dict[str, Any]]] = {}
        for row in option_rows:
            payload = dict(row)
            question_id = int(payload.get("question_id") or 0)
            options_by_question.setdefault(question_id, []).append(
                {
                    "id": int(payload["id"]),
                    "label": _text(payload.get("option_text")),
                    "value": _text(payload.get("id")),
                    "option_text": _text(payload.get("option_text")),
                    "score": int(float(payload.get("score") or 0)),
                    "tag_codes": _json_list(payload.get("tag_codes")),
                    "is_other": bool(payload.get("is_other")),
                    "other_placeholder": _text(payload.get("other_placeholder")),
                    "other_max_length": int(payload.get("other_max_length") or 80),
                    "sort_order": int(payload.get("sort_order") or 0),
                }
            )
        questions: list[dict[str, Any]] = []
        for row in question_rows:
            payload = dict(row)
            question_id = int(payload["id"])
            questions.append(
                {
                    "id": question_id,
                    "type": _text(payload.get("type") or "single_choice"),
                    "title": _text(payload.get("title")),
                    "required": bool(payload.get("required")),
                    "placeholder_text": _text(payload.get("placeholder_text")),
                    "assessment_dimension_key": _text(payload.get("assessment_dimension_key")),
                    "sidebar_profile_field": _text(payload.get("sidebar_profile_field")),
                    "sort_order": int(payload.get("sort_order") or 0),
                    "created_at": _timestamp(payload.get("created_at")),
                    "updated_at": _timestamp(payload.get("updated_at")),
                    "options": options_by_question.get(question_id, []),
                }
            )
        return questions

    def get_results_summary(self, questionnaire_id: int) -> dict[str, Any] | None:
        if not self._exists(questionnaire_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS submission_count,
                    MAX(submitted_at) AS latest_submitted_at,
                    COALESCE(AVG(total_score), 0) AS average_score
                FROM questionnaire_submissions
                WHERE questionnaire_id = %s
                """,
                (int(questionnaire_id),),
            ).fetchone()
        return {
            "questionnaire_id": int(questionnaire_id),
            "submission_count": int((row or {}).get("submission_count") or 0),
            "latest_submitted_at": _timestamp((row or {}).get("latest_submitted_at")),
            "average_score": float((row or {}).get("average_score") or 0),
            "rules": self._list_score_rules(questionnaire_id),
        }

    def list_submissions(self, questionnaire_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int] | None:
        if not self._exists(questionnaire_id):
            return None
        with self._connect() as conn:
            total = int(
                (
                    conn.execute("SELECT COUNT(*) AS total FROM questionnaire_submissions WHERE questionnaire_id = %s", (int(questionnaire_id),)).fetchone()
                    or {}
                ).get("total")
                or 0
            )
            rows = conn.execute(
                """
                SELECT qs.id, qs.questionnaire_id, '' AS respondent_key,
                       COALESCE(identity.primary_openid, '') AS openid,
                       qs.unionid,
                       COALESCE(identity.primary_external_userid, '') AS external_userid,
                       COALESCE(
                           NULLIF(NULLIF(identity.customer_name, ''), '问卷提交用户'),
                           NULLIF(identity.profile_json ->> 'name', ''),
                           ''
                       ) AS customer_name,
                       qs.follow_user_userid, '' AS matched_by,
                       COALESCE(identity.mobile, '') AS mobile_snapshot,
                       qs.source_channel, qs.campaign_id,
                       qs.staff_id, qs.total_score, qs.final_tags, qs.result_token,
                       '' AS redirect_url_snapshot,
                       qs.submitted_at
                FROM questionnaire_submissions qs
                LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                WHERE qs.questionnaire_id = %s
                ORDER BY qs.submitted_at DESC, qs.id DESC
                LIMIT %s OFFSET %s
                """,
                (int(questionnaire_id), int(limit), int(offset)),
            ).fetchall()
            row_ids = [int(row["id"]) for row in rows]
            answer_rows = []
            if row_ids:
                answer_rows = conn.execute(
                    """
                    SELECT submission_id, question_id, question_type, question_title_snapshot,
                           selected_option_ids, selected_option_texts_snapshot, text_value
                    FROM questionnaire_submission_answers
                    WHERE submission_id = ANY(%s)
                    ORDER BY submission_id ASC, id ASC
                    """,
                    (row_ids,),
                ).fetchall()
        answers_by_submission: dict[int, dict[str, Any]] = {}
        answer_snapshots_by_submission: dict[int, list[dict[str, Any]]] = {}
        for answer in answer_rows:
            submission_id = int(answer.get("submission_id") or 0)
            answer_payload = dict(answer)
            answer_snapshots_by_submission.setdefault(submission_id, []).append(answer_payload)
            key = str(answer_payload.get("question_id"))
            answers = answers_by_submission.setdefault(submission_id, {})
            if answer_payload.get("question_type") in {"textarea", "mobile"}:
                answers[key] = _text(answer_payload.get("text_value"))
                continue
            selected = _json_list(answer_payload.get("selected_option_ids"))
            other_text = _text(answer_payload.get("text_value")).strip()
            if other_text:
                answers[key] = {"selected_option_ids": selected, "other_text": other_text}
            else:
                answers[key] = selected[0] if len(selected) == 1 else selected
        items = [
            {
                **dict(row),
                "submission_id": str(row.get("id")),
                "submitted_at": _timestamp(row.get("submitted_at")),
                "final_tags": _json_list(row.get("final_tags")),
                "score": float(row.get("total_score") or 0),
                "mobile": _text(row.get("mobile_snapshot")),
                "answers": answers_by_submission.get(int(row.get("id") or 0), {}),
                "answer_snapshots": answer_snapshots_by_submission.get(int(row.get("id") or 0), []),
            }
            for row in rows
        ]
        return items, total

    def list_external_submissions(
        self,
        *,
        filters: dict[str, Any],
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if _text(filters.get("mobile")).strip():
            clauses.append("(identity.mobile = %s OR identity.mobile_normalized = %s)")
            mobile = _text(filters.get("mobile")).strip()
            params.extend([mobile, mobile])
        if _text(filters.get("unionid")).strip():
            clauses.append("qs.unionid = %s")
            params.append(_text(filters.get("unionid")).strip())
        external_userid = _text(filters.get("external_userid")).strip()
        if filters.get("questionnaire_id") not in (None, ""):
            clauses.append("qs.questionnaire_id = %s")
            params.append(int(filters.get("questionnaire_id") or 0))
        if _text(filters.get("submitted_from")).strip():
            clauses.append("qs.submitted_at >= %s")
            params.append(_text(filters.get("submitted_from")).strip())
        if _text(filters.get("submitted_to")).strip():
            clauses.append("qs.submitted_at <= %s")
            params.append(_text(filters.get("submitted_to")).strip())

        with self._connect() as conn:
            if external_userid:
                identity = conn.execute(
                    """
                    SELECT unionid
                    FROM crm_user_identity
                    WHERE primary_external_userid = %s
                       OR external_userids_json @> jsonb_build_array(CAST(%s AS text))
                       OR external_userids_json @> jsonb_build_array(
                            jsonb_build_object('external_userid', CAST(%s AS text))
                       )
                    LIMIT 1
                    """,
                    (external_userid, external_userid, external_userid),
                ).fetchone()
                resolved_unionid = _text((identity or {}).get("unionid"))
                if not resolved_unionid:
                    return [], 0
                clauses.append("qs.unionid = %s AND qs.unionid <> ''")
                params.append(resolved_unionid)

            where_sql = " AND ".join(clauses)
            total = int(
                (
                    conn.execute(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM questionnaire_submissions qs
                        LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                        WHERE {where_sql}
                        """,
                        tuple(params),
                    ).fetchone()
                    or {}
                ).get("total")
                or 0
            )
            rows = conn.execute(
                f"""
                SELECT
                    qs.id,
                    qs.questionnaire_id,
                    qs.unionid,
                    COALESCE(identity.primary_external_userid, '') AS external_userid,
                    COALESCE(identity.mobile, '') AS mobile_snapshot,
                    qs.submitted_at,
                    qs.final_tags,
                    qs.assessment_result_snapshot,
                    COALESCE(NULLIF(q.title, ''), NULLIF(q.name, ''), '') AS questionnaire_title
                FROM questionnaire_submissions qs
                LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                LEFT JOIN questionnaires q ON q.id = qs.questionnaire_id
                WHERE {where_sql}
                ORDER BY qs.submitted_at DESC, qs.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [int(limit), int(offset)]),
            ).fetchall()
            row_ids = [int(row["id"]) for row in rows]
            answer_rows = []
            if row_ids:
                answer_rows = conn.execute(
                    """
                    SELECT submission_id, question_title_snapshot, selected_option_texts_snapshot,
                           text_value, score_contribution
                    FROM questionnaire_submission_answers
                    WHERE submission_id = ANY(%s)
                    ORDER BY submission_id ASC, id ASC
                    """,
                    (row_ids,),
                ).fetchall()

        answers_by_submission: dict[int, list[dict[str, Any]]] = {}
        for answer in answer_rows:
            answers_by_submission.setdefault(int(answer.get("submission_id") or 0), []).append(dict(answer))
        items = [_external_submission_projection(dict(row), answers_by_submission.get(int(row.get("id") or 0), [])) for row in rows]
        return items, total

    def _exists(self, questionnaire_id: int) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("SELECT 1 FROM questionnaires WHERE id = %s", (int(questionnaire_id),)).fetchone())

    def _list_score_rules(self, questionnaire_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, questionnaire_id, min_score, max_score, tag_codes, sort_order, created_at, updated_at
                FROM questionnaire_score_rules
                WHERE questionnaire_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (int(questionnaire_id),),
            ).fetchall()
        return [
            {
                **dict(row),
                "tag_codes": _json_list(row.get("tag_codes")),
                "created_at": _timestamp(row.get("created_at")),
                "updated_at": _timestamp(row.get("updated_at")),
            }
            for row in rows
        ]

    def save_questionnaire(self, payload: dict[str, Any], questionnaire_id: int | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                existing = None
                if questionnaire_id is not None:
                    existing = conn.execute(
                        "SELECT id, slug, name, title FROM questionnaires WHERE id = %s FOR UPDATE",
                        (int(questionnaire_id),),
                    ).fetchone()
                    if not existing:
                        return {}
                requested_slug = _text(payload.get("slug")).strip()
                slug_source = requested_slug or _text((existing or {}).get("slug")) or _text(payload.get("name")) or _text(payload.get("title"))
                slug = _slugify_questionnaire(slug_source)
                if self._slug_exists(conn, slug, exclude_id=int(questionnaire_id) if questionnaire_id is not None else None):
                    if requested_slug:
                        raise RepositoryProviderError("slug already exists")
                    slug = self._dedupe_slug(conn, slug_source, exclude_id=int(questionnaire_id) if questionnaire_id is not None else None)
                normalized = _questionnaire_payload(payload, slug=slug)
                if not normalized["name"]:
                    raise RepositoryProviderError("name is required")
                if not normalized["title"]:
                    raise RepositoryProviderError("title is required")
                external_push = normalized["external_push"]

                if questionnaire_id is None:
                    row = conn.execute(
                        """
                        INSERT INTO questionnaires (
                            slug, name, title, description, is_disabled, redirect_url, completion_target_json, lead_channel_id,
                            answer_display_mode, assessment_enabled, assessment_config,
                            external_push_enabled, external_push_url, external_push_type, external_push_expires_at_ts,
                            external_push_day, external_push_frequency, external_push_remark, external_push_custom_params,
                            created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        RETURNING id
                        """,
                        (
                            normalized["slug"],
                            normalized["name"],
                            normalized["title"],
                            normalized["description"],
                            normalized["is_disabled"],
                            normalized["redirect_url"],
                            _jsonb(normalized["completion_target_json"]),
                            int(payload.get("lead_channel_id") or 0) or None,
                            normalized["answer_display_mode"],
                            normalized["assessment_enabled"],
                            _jsonb(normalized["assessment_config"]),
                            external_push["enabled"],
                            external_push["url"],
                            external_push["type"],
                            external_push["expires_at_ts"],
                            external_push["day"],
                            external_push["frequency"],
                            external_push["remark"],
                            _jsonb(external_push["custom_params"]),
                        ),
                    ).fetchone()
                    questionnaire_id = int(row["id"])
                else:
                    conn.execute(
                        """
                        UPDATE questionnaires
                        SET slug = %s, name = %s, title = %s, description = %s, is_disabled = %s,
                            redirect_url = %s, completion_target_json = %s, answer_display_mode = %s, assessment_enabled = %s,
                            assessment_config = %s, external_push_enabled = %s, external_push_url = %s,
                            external_push_type = %s, external_push_expires_at_ts = %s, external_push_day = %s,
                            external_push_frequency = %s, external_push_remark = %s,
                            external_push_custom_params = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            normalized["slug"],
                            normalized["name"],
                            normalized["title"],
                            normalized["description"],
                            normalized["is_disabled"],
                            normalized["redirect_url"],
                            _jsonb(normalized["completion_target_json"]),
                            normalized["answer_display_mode"],
                            normalized["assessment_enabled"],
                            _jsonb(normalized["assessment_config"]),
                            external_push["enabled"],
                            external_push["url"],
                            external_push["type"],
                            external_push["expires_at_ts"],
                            external_push["day"],
                            external_push["frequency"],
                            external_push["remark"],
                            _jsonb(external_push["custom_params"]),
                            int(questionnaire_id),
                        ),
                    )
                self._sync_questions(conn, int(questionnaire_id), _json_list(payload.get("questions")))
                self._sync_score_rules(conn, int(questionnaire_id), _json_list(payload.get("score_rules") or payload.get("rules")))
        item = self.get_questionnaire(int(questionnaire_id))
        if not item:
            raise RepositoryProviderError("questionnaire write failed")
        return item

    def save_completion_operations(
        self,
        questionnaire_id: int,
        *,
        lead_channel_id: int | None,
        lead_qr_title: str,
        lead_qr_subtitle: str,
        completion_target_json: dict[str, Any],
        redirect_url: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE questionnaires
                    SET lead_channel_id = %s,
                        lead_qr_title = %s,
                        lead_qr_subtitle = %s,
                        completion_target_json = %s,
                        redirect_url = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        int(lead_channel_id or 0) or None,
                        _text(lead_qr_title).strip(),
                        _text(lead_qr_subtitle).strip(),
                        _jsonb(completion_target_json),
                        _text(redirect_url),
                        int(questionnaire_id),
                    ),
                ).fetchone()
        return self.get_questionnaire(int(questionnaire_id)) if row else None

    def save_external_push_operations(
        self,
        questionnaire_id: int,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE questionnaires
                    SET external_push_enabled = %s,
                        external_push_url = %s,
                        external_push_type = %s,
                        external_push_expires_at_ts = %s,
                        external_push_day = %s,
                        external_push_frequency = %s,
                        external_push_remark = %s,
                        external_push_custom_params = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        bool(config.get("enabled")),
                        _text(config.get("webhook_url")),
                        _text(config.get("type")),
                        config.get("expires_at_ts"),
                        config.get("day"),
                        config.get("frequency"),
                        _text(config.get("remark")),
                        _jsonb(_json_list(config.get("custom_params"))),
                        int(questionnaire_id),
                    ),
                ).fetchone()
        return self.get_questionnaire(int(questionnaire_id)) if row else None

    def set_enabled(self, questionnaire_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE questionnaires
                    SET is_disabled = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (not bool(enabled), int(questionnaire_id)),
                ).fetchone()
        if not row:
            return None
        return self.get_questionnaire(int(questionnaire_id))

    def delete_questionnaire(self, questionnaire_id: int) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    DELETE FROM questionnaires
                    WHERE id = %s
                    RETURNING id
                    """,
                    (int(questionnaire_id),),
                ).fetchone()
        return bool(row)

    def _slug_exists(self, conn: Any, slug: str, *, exclude_id: int | None = None) -> bool:
        params: list[Any] = [slug]
        sql = "SELECT 1 FROM questionnaires WHERE slug = %s"
        if exclude_id is not None:
            sql += " AND id <> %s"
            params.append(int(exclude_id))
        return bool(conn.execute(sql, tuple(params)).fetchone())

    def _dedupe_slug(self, conn: Any, slug_source: str, *, exclude_id: int | None = None) -> str:
        candidate = _slugify_questionnaire(slug_source)
        if not self._slug_exists(conn, candidate, exclude_id=exclude_id):
            return candidate
        while True:
            suffix = uuid4().hex[:6]
            prefix = candidate[: max(120 - len(suffix) - 1, 1)].rstrip("-")
            fallback_prefix = datetime.now(timezone.utc).strftime("q-%Y%m%d%H%M%S")
            deduped = f"{prefix or fallback_prefix}-{suffix}"[:120]
            if not self._slug_exists(conn, deduped, exclude_id=exclude_id):
                return deduped

    def _sync_questions(self, conn: Any, questionnaire_id: int, questions: list[Any]) -> None:
        conn.execute("DELETE FROM questionnaire_questions WHERE questionnaire_id = %s", (int(questionnaire_id),))
        for index, raw_question in enumerate(questions, start=1):
            question = dict(raw_question or {})
            question_type = _text(question.get("type") or "single_choice")
            title = _text(question.get("title"))
            if not title:
                raise RepositoryProviderError("question title is required")
            row = conn.execute(
                """
                INSERT INTO questionnaire_questions (
                    questionnaire_id, type, title, placeholder_text, assessment_dimension_key,
                    sidebar_profile_field, required, sort_order, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                RETURNING id
                """,
                (
                    int(questionnaire_id),
                    question_type,
                    title,
                    _text(question.get("placeholder_text")),
                    _text(question.get("assessment_dimension_key")),
                    _text(question.get("sidebar_profile_field")),
                    _as_bool(question.get("required")),
                    int(question.get("sort_order") or index),
                ),
            ).fetchone()
            question_id = int(row["id"])
            if question_type not in {"textarea", "mobile"}:
                self._insert_options(conn, question_id, _json_list(question.get("options")))

    def _insert_options(self, conn: Any, question_id: int, options: list[Any]) -> None:
        for index, raw_option in enumerate(options, start=1):
            option = dict(raw_option or {})
            option_text = _text(option.get("option_text") or option.get("label") or option.get("value"))
            if not option_text:
                raise RepositoryProviderError("option_text is required")
            conn.execute(
                """
                INSERT INTO questionnaire_options (
                    question_id, option_text, score, assessment_type_key, tag_codes,
                    is_other, other_placeholder, other_max_length, sort_order, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    int(question_id),
                    option_text,
                    float(option.get("score") or 0),
                    _text(option.get("assessment_type_key")),
                    _jsonb(_json_list(option.get("tag_codes"))),
                    _as_bool(option.get("is_other")),
                    _text(option.get("other_placeholder")),
                    int(option.get("other_max_length") or 80),
                    int(option.get("sort_order") or index),
                ),
            )

    def _sync_score_rules(self, conn: Any, questionnaire_id: int, score_rules: list[Any]) -> None:
        conn.execute("DELETE FROM questionnaire_score_rules WHERE questionnaire_id = %s", (int(questionnaire_id),))
        for index, raw_rule in enumerate(score_rules, start=1):
            rule = dict(raw_rule or {})
            conn.execute(
                """
                INSERT INTO questionnaire_score_rules (
                    questionnaire_id, min_score, max_score, tag_codes, sort_order, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    int(questionnaire_id),
                    _optional_float(rule.get("min_score")),
                    _optional_float(rule.get("max_score")),
                    _jsonb(_json_list(rule.get("tag_codes"))),
                    int(rule.get("sort_order") or index),
                ),
            )

    def create_submission(
        self,
        payload: dict[str, Any],
        *,
        internal_event_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        questionnaire_id = int(payload.get("questionnaire_id") or 0)
        if not questionnaire_id:
            raise RepositoryProviderError("questionnaire_id is required for questionnaire submit")
        answers = _json_dict(payload.get("answers") or payload.get("answers_json"))
        questions = self.list_questions(questionnaire_id) or []
        answer_snapshots = _answer_snapshots(questions, answers)
        source = _json_dict(payload.get("source_json"))
        respondent_identity = _json_dict(payload.get("respondent_identity"))
        mobile_snapshot = _text(payload.get("mobile") or respondent_identity.get("mobile") or _mobile_answer(questions, answers)).strip()
        final_tags = _json_list(payload.get("final_tags"))
        score = float(payload.get("score") or (payload.get("result_json") or {}).get("score") or 0)
        assessment_result = _json_dict((payload.get("result_json") or {}).get("assessment_result"))
        requested_identity = ResolvePersonIdentityRequest(
            unionid=_text(payload.get("unionid") or respondent_identity.get("unionid")) or None,
            external_userid=_text(payload.get("external_userid") or respondent_identity.get("external_userid")) or None,
            openid=_text(payload.get("openid") or respondent_identity.get("openid")) or None,
            mobile=mobile_snapshot or None,
        )
        customer_id = int(payload.get("customer_id") or respondent_identity.get("customer_id") or 0)
        respondent_identity_id = int(payload.get("respondent_identity_id") or respondent_identity.get("respondent_identity_id") or 0)
        with self._connect() as identity_conn:
            identity_resolution = resolve_identity_with_dbapi(identity_conn, requested_identity)
            if customer_id or respondent_identity_id:
                trusted_identity = identity_conn.execute(
                    """
                    SELECT identity.id AS identity_id,
                           aicrm_customer_root_id(identity.customer_id) AS customer_id
                    FROM customer_identities identity
                    WHERE identity.id = %s AND identity.status = 'active'
                    LIMIT 1
                    """,
                    (respondent_identity_id,),
                ).fetchone()
                if not trusted_identity or int(trusted_identity.get("customer_id") or 0) != customer_id:
                    raise ContractError("questionnaire_identity_invalid")
            elif requested_identity.external_userid:
                customer_row = identity_conn.execute(
                    """
                    SELECT COALESCE(root.id, customer.id) AS customer_id
                    FROM wecom_external_contact_identity_map identity_map
                    JOIN customers customer ON customer.id = identity_map.customer_id
                    LEFT JOIN customers root ON root.id = customer.merged_into_customer_id
                    WHERE identity_map.external_userid = %s
                      AND identity_map.status = 'active'
                    ORDER BY identity_map.updated_at DESC, identity_map.id DESC
                    LIMIT 1
                    """,
                    (requested_identity.external_userid,),
                ).fetchone()
                customer_id = int((customer_row or {}).get("customer_id") or 0)
        unionid = resolved_unionid(identity_resolution)
        resolution_queue_payload: dict[str, Any] | None = None
        if not unionid and not customer_id:
            resolution_queue_payload = {
                "source_type": "questionnaire_submission",
                "questionnaire_id": questionnaire_id,
                "respondent_key": _text(payload.get("respondent_key") or respondent_identity.get("respondent_key")),
                "openid": _text(payload.get("openid") or respondent_identity.get("openid")),
                "external_userid": _text(payload.get("external_userid") or respondent_identity.get("external_userid")),
                "mobile": mobile_snapshot,
                "slug": _text(payload.get("slug")),
            }
            if identity_resolution.status == "conflict":
                with self._connect() as conflict_conn:
                    with conflict_conn.transaction():
                        enqueue_questionnaire_identity_resolution(
                            conflict_conn,
                            resolution_queue_payload,
                            reason="identity_conflict",
                        )
                raise ContractError("identity_conflict")
            if not any(
                (
                    requested_identity.unionid,
                    requested_identity.external_userid,
                    requested_identity.openid,
                    requested_identity.mobile,
                )
            ):
                resolution_queue_payload = None

        submission: dict[str, Any] = {}
        with self._connect() as conn:
            with conn.transaction():
                if customer_id or unionid:
                    lock_key = f"questionnaire_submission:{questionnaire_id}:{customer_id or unionid}"
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                    if customer_id:
                        existing = conn.execute(
                            """
                            SELECT id
                            FROM questionnaire_submissions
                            WHERE questionnaire_id = %s AND customer_id = %s
                            LIMIT 1
                            """,
                            (questionnaire_id, customer_id),
                        ).fetchone()
                    else:
                        existing = conn.execute(
                            """
                            SELECT id
                            FROM questionnaire_submissions
                            WHERE questionnaire_id = %s AND unionid = %s
                            LIMIT 1
                            """,
                            (questionnaire_id, unionid),
                        ).fetchone()
                    if existing:
                        raise ContractError("already_submitted")
                if resolution_queue_payload is not None:
                    enqueue_questionnaire_identity_resolution(
                        conn,
                        resolution_queue_payload,
                        reason="missing_unionid",
                    )
                row = conn.execute(
                    """
                    INSERT INTO questionnaire_submissions (
                        questionnaire_id, customer_id, respondent_identity_id, unionid,
                        follow_user_userid, source_channel, campaign_id,
                        staff_id, total_score, final_tags, assessment_result_snapshot, result_token, submitted_at
                    )
                    VALUES (%s, NULLIF(%s, 0), NULLIF(%s, 0), %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id, submitted_at
                    """,
                    (
                        questionnaire_id,
                        customer_id,
                        respondent_identity_id,
                        unionid or "",
                        _text(payload.get("follow_user_userid")),
                        _text(source.get("source_channel")),
                        _text(source.get("campaign_id")),
                        _text(source.get("staff_id")),
                        score,
                        _jsonb(final_tags),
                        _jsonb(assessment_result),
                        _text(payload.get("result_token")),
                    ),
                ).fetchone()
                submission_id = int(row["id"])
                for item in answer_snapshots:
                    conn.execute(
                        """
                        INSERT INTO questionnaire_submission_answers (
                            submission_id, question_id, question_type, question_title_snapshot,
                            selected_option_ids, selected_option_texts_snapshot, selected_option_scores_snapshot,
                            selected_option_tags_snapshot, text_value, score_contribution, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            submission_id,
                            int(item["question_id"]),
                            item["question_type"],
                            item["question_title_snapshot"],
                            _jsonb(item.get("selected_option_ids") or []),
                            _jsonb(item.get("selected_option_texts_snapshot") or []),
                            _jsonb(item.get("selected_option_scores_snapshot") or []),
                            _jsonb(item.get("selected_option_tags_snapshot") or []),
                            item.get("text_value", "") or "",
                            float(item.get("score_contribution") or 0),
                        ),
                    )
                submitted_at = _timestamp(row.get("submitted_at"))
                submission = {
                    "id": submission_id,
                    "submission_id": str(submission_id),
                    "result_token": _text(payload.get("result_token")),
                    "questionnaire_id": questionnaire_id,
                    "customer_id": customer_id,
                    "respondent_identity_id": respondent_identity_id,
                    "slug": _text(payload.get("slug")),
                    "answers": answers,
                    "answers_json": answers,
                    "result_json": _json_dict(payload.get("result_json")),
                    "source_json": source,
                    "diagnostics_json": _json_dict(payload.get("diagnostics_json")),
                    "respondent_identity": respondent_identity,
                    "person_id": payload.get("person_id"),
                    "identity_map_id": payload.get("identity_map_id"),
                    "respondent_key": _text(payload.get("respondent_key") or respondent_identity.get("respondent_key")),
                    "external_userid": _text(payload.get("external_userid") or respondent_identity.get("external_userid")),
                    "follow_user_userid": _text(payload.get("follow_user_userid")),
                    "matched_by": _text(payload.get("matched_by")),
                    "openid": _text(payload.get("openid") or respondent_identity.get("openid")),
                    "unionid": unionid,
                    "mobile": mobile_snapshot,
                    "mobile_snapshot": mobile_snapshot,
                    "source_channel": _text(source.get("source_channel")),
                    "campaign_id": _text(source.get("campaign_id")),
                    "staff_id": _text(source.get("staff_id")),
                    "binding_status": _text(payload.get("binding_status") or "unresolved"),
                    "score": score,
                    "total_score": score,
                    "final_tags": final_tags,
                    "status": _text(payload.get("status") or "submitted"),
                    "created_at": submitted_at,
                    "submitted_at": submitted_at,
                    "updated_at": _text(payload.get("updated_at") or submitted_at),
                    "answer_snapshots": answer_snapshots,
                }
                if internal_event_factory is not None:
                    request = internal_event_factory(dict(submission))
                    if request is None:
                        raise RepositoryProviderError("questionnaire.submitted event identity is incomplete")
                    submission["internal_event_outbox"] = enqueue_transactional_internal_event_outbox(conn, request)
        return submission

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        normalized_id = str(submission_id or "").strip()
        if not normalized_id:
            return None
        return self._get_submission_by("result_token", normalized_id)

    def get_submission_by_record_id(self, submission_id: str) -> dict[str, Any] | None:
        normalized_id = str(submission_id or "").strip()
        if not normalized_id or not normalized_id.isdigit():
            return None
        return self._get_submission_by("record_id", int(normalized_id))

    def _get_submission_by(self, lookup: str, value: Any) -> dict[str, Any] | None:
        if lookup == "result_token":
            query = """
                SELECT qs.*, q.slug,
                       COALESCE(identity.primary_external_userid, '') AS canonical_external_userid,
                       COALESCE(identity.primary_openid, '') AS canonical_openid,
                       COALESCE(identity.mobile, '') AS canonical_mobile,
                       COALESCE(identity.primary_owner_userid, '') AS canonical_owner_userid
                FROM questionnaire_submissions qs
                JOIN questionnaires q ON q.id = qs.questionnaire_id
                LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                WHERE qs.result_token = %s
                LIMIT 1
            """
        elif lookup == "record_id":
            query = """
                SELECT qs.*, q.slug,
                       COALESCE(identity.primary_external_userid, '') AS canonical_external_userid,
                       COALESCE(identity.primary_openid, '') AS canonical_openid,
                       COALESCE(identity.mobile, '') AS canonical_mobile,
                       COALESCE(identity.primary_owner_userid, '') AS canonical_owner_userid
                FROM questionnaire_submissions qs
                JOIN questionnaires q ON q.id = qs.questionnaire_id
                LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                WHERE qs.id = %s
                LIMIT 1
            """
        else:
            raise ValueError(f"unsupported questionnaire submission lookup: {lookup}")
        with self._connect() as conn:
            row = conn.execute(
                query,
                (value,),
            ).fetchone()
            if not row:
                return None
            answer_rows = conn.execute(
                """
                SELECT question_id, question_type, question_title_snapshot,
                       selected_option_ids, selected_option_texts_snapshot, text_value
                FROM questionnaire_submission_answers
                WHERE submission_id = %s
                ORDER BY id ASC
                """,
                (int(row["id"]),),
            ).fetchall()
        answers: dict[str, Any] = {}
        answer_snapshots: list[dict[str, Any]] = []
        for answer in answer_rows:
            answer_payload = dict(answer)
            answer_snapshots.append(answer_payload)
            key = str(answer_payload.get("question_id"))
            if answer_payload.get("question_type") in {"textarea", "mobile"}:
                answers[key] = _text(answer_payload.get("text_value"))
            else:
                selected = _json_list(answer_payload.get("selected_option_ids"))
                other_text = _text(answer_payload.get("text_value")).strip()
                if other_text:
                    answers[key] = {"selected_option_ids": selected, "other_text": other_text}
                else:
                    answers[key] = selected[0] if len(selected) == 1 else selected
        return {
            **dict(row),
            "submission_id": str(row.get("id")),
            "slug": _text(row.get("slug")),
            "answers": answers,
            "score": float(row.get("total_score") or 0),
            "final_tags": _json_list(row.get("final_tags")),
            "external_userid": _text(row.get("canonical_external_userid")),
            "openid": _text(row.get("canonical_openid")),
            "follow_user_userid": _text(row.get("follow_user_userid") or row.get("canonical_owner_userid")),
            "mobile": _text(row.get("canonical_mobile")),
            "created_at": _timestamp(row.get("submitted_at")),
            "submitted_at": _timestamp(row.get("submitted_at")),
            "answer_snapshots": answer_snapshots,
        }

    def find_submission_for_identity(self, questionnaire_id: int, identity: dict[str, Any]) -> dict[str, Any] | None:
        customer_id = int(identity.get("customer_id") or 0)
        request = ResolvePersonIdentityRequest(
            unionid=_text(identity.get("unionid")) or None,
            external_userid=_text(identity.get("external_userid")) or None,
            openid=_text(identity.get("openid")) or None,
            mobile=_text(identity.get("mobile")) or None,
        )
        if not customer_id and not any((request.unionid, request.external_userid, request.openid, request.mobile)):
            return None
        with self._connect() as conn:
            if customer_id:
                root_customer_id = conn.execute(
                    "SELECT aicrm_customer_root_id(%s) AS customer_id",
                    (customer_id,),
                ).fetchone()
                row = conn.execute(
                    """
                    SELECT qs.id, qs.questionnaire_id, '' AS respondent_key,
                           COALESCE(identity.primary_openid, '') AS openid,
                           qs.unionid,
                           COALESCE(identity.primary_external_userid, '') AS external_userid,
                           COALESCE(identity.mobile, '') AS mobile_snapshot,
                           qs.total_score, qs.final_tags, qs.result_token,
                           '' AS redirect_url_snapshot,
                           qs.submitted_at
                    FROM questionnaire_submissions qs
                    LEFT JOIN crm_user_identity identity ON identity.customer_id = qs.customer_id
                    WHERE qs.questionnaire_id = %s AND qs.customer_id = %s
                    ORDER BY qs.submitted_at DESC, qs.id DESC
                    LIMIT 1
                    """,
                    (int(questionnaire_id), int((root_customer_id or {}).get("customer_id") or customer_id)),
                ).fetchone()
                if row:
                    return {
                        **dict(row),
                        "submission_id": str(row.get("id")),
                        "mobile": _text(row.get("mobile_snapshot")),
                        "score": float(row.get("total_score") or 0),
                        "final_tags": _json_list(row.get("final_tags")),
                        "submitted_at": _timestamp(row.get("submitted_at")),
                    }
            resolution = resolve_identity_with_dbapi(conn, request)
            unionid = resolved_unionid(resolution)
            if not unionid:
                return None
            row = conn.execute(
                """
                SELECT qs.id, qs.questionnaire_id, '' AS respondent_key,
                       COALESCE(identity.primary_openid, '') AS openid,
                       qs.unionid,
                       COALESCE(identity.primary_external_userid, '') AS external_userid,
                       COALESCE(identity.mobile, '') AS mobile_snapshot,
                       qs.total_score, qs.final_tags, qs.result_token,
                       '' AS redirect_url_snapshot,
                       qs.submitted_at
                FROM questionnaire_submissions qs
                LEFT JOIN crm_user_identity identity ON identity.unionid = qs.unionid
                WHERE qs.questionnaire_id = %s AND qs.unionid = %s
                ORDER BY qs.submitted_at DESC, qs.id DESC
                LIMIT 1
                """,
                (int(questionnaire_id), unionid),
            ).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "submission_id": str(row.get("id")),
            "mobile": _text(row.get("mobile_snapshot")),
            "score": float(row.get("total_score") or 0),
            "final_tags": _json_list(row.get("final_tags")),
            "submitted_at": _timestamp(row.get("submitted_at")),
        }

    def latest_submission(self, questionnaire_id: int) -> dict[str, Any] | None:
        submissions = self.list_submissions(questionnaire_id, limit=1, offset=0)
        if not submissions or not submissions[0]:
            return None
        return submissions[0][0]

    def export_submissions(self, questionnaire_id: int) -> dict[str, Any] | None:
        raise RepositoryProviderError("questionnaire export remains out of scope for the admin read replacement")

    def get_app_setting(self, key: str) -> str | None:
        return runtime_setting(key, "") or None

    def list_external_push_log_threads(
        self,
        questionnaire_id: int | None = None,
        *,
        questionnaire_title: str = "",
        user_id: str = "",
        target_url: str = "",
        status: str = "",
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, questionnaire_id, questionnaire_title_snapshot, submission_record_id,
                   retry_from_log_id, retry_attempt, user_id, target_url, request_payload,
                   response_status_code, response_body, status, failure_reason, created_at, updated_at
            FROM questionnaire_external_push_logs
            WHERE 1 = 1
        """
        params: list[Any] = []
        if questionnaire_id is not None:
            sql += " AND questionnaire_id = %s"
            params.append(int(questionnaire_id))
        if _text(questionnaire_title).strip():
            sql += " AND questionnaire_title_snapshot ILIKE %s"
            params.append(f"%{_text(questionnaire_title).strip()}%")
        if _text(user_id).strip():
            sql += " AND user_id ILIKE %s"
            params.append(f"%{_text(user_id).strip()}%")
        if _text(target_url).strip():
            sql += " AND target_url ILIKE %s"
            params.append(f"%{_text(target_url).strip()}%")
        sql += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        return _external_push_log_threads(rows, status=status, limit=limit)

    def count_external_push_logs(
        self,
        *,
        questionnaire_id: int | None = None,
        questionnaire_title: str = "",
        user_id: str = "",
        target_url: str = "",
        status: str = "",
        created_at_gte: str = "",
    ) -> int:
        sql = "SELECT COUNT(*) AS total FROM questionnaire_external_push_logs WHERE 1 = 1"
        params: list[Any] = []
        if questionnaire_id is not None:
            sql += " AND questionnaire_id = %s"
            params.append(int(questionnaire_id))
        if _text(questionnaire_title).strip():
            sql += " AND questionnaire_title_snapshot ILIKE %s"
            params.append(f"%{_text(questionnaire_title).strip()}%")
        if _text(user_id).strip():
            sql += " AND user_id ILIKE %s"
            params.append(f"%{_text(user_id).strip()}%")
        if _text(target_url).strip():
            sql += " AND target_url ILIKE %s"
            params.append(f"%{_text(target_url).strip()}%")
        if _text(status).strip():
            sql += " AND status = %s"
            params.append(_text(status).strip())
        if _text(created_at_gte).strip():
            sql += " AND created_at >= %s"
            params.append(_text(created_at_gte).strip())
        with self._connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone() or {}
        return int(row.get("total") or 0)

    def summarize_external_push_logs(self, questionnaire_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = (
                conn.execute(
                    """
                SELECT COUNT(*) AS total_count,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                       MAX(created_at) AS last_created_at
                FROM questionnaire_external_push_logs
                WHERE questionnaire_id = %s
                """,
                    (int(questionnaire_id),),
                ).fetchone()
                or {}
            )
        return {
            "total_count": int(row.get("total_count") or 0),
            "success_count": int(row.get("success_count") or 0),
            "failed_count": int(row.get("failed_count") or 0),
            "last_created_at": _timestamp(row.get("last_created_at")),
        }


_DEFAULT_REPO = InMemoryQuestionnaireRepository()


def build_questionnaire_repository() -> QuestionnaireRepository:
    if production_data_ready():
        return assert_repository_allowed(PostgresQuestionnaireReadRepository(), capability_owner="questionnaire")
    return assert_repository_allowed(_DEFAULT_REPO, capability_owner="questionnaire")


def reset_questionnaire_fixture_state() -> None:
    _DEFAULT_REPO.reset()
