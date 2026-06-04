-- ============================================================
-- TDA Dashboard r223 — 연구 정련소 (Research Refinery) 마이그레이션
-- ============================================================
-- 실행 위치: Supabase Dashboard → SQL Editor → New Query
-- 실행 방법: 전체 복사 → 붙여넣기 → "Run" 클릭
-- 멱등성: 모든 명령은 IF NOT EXISTS / OR REPLACE 사용 → 여러 번 실행 안전
--
-- 스펙: docs/연구정련소-스펙.md v0.2
-- 작성자 정책: created_by 누락 0 — 백엔드 _author_guard 가 422 차단,
--              여기서는 NOT NULL 제약 강제
-- ============================================================

-- ============================================================
-- 0. 사전 정리 — 기존 wiki_docs 의 created_by NULL 인 행 마이그레이션
-- ============================================================
-- ★ 이 블록은 실행 전 반드시 확인:
--   1) NULL 카운트 보기:
--      SELECT count(*) FROM public.wiki_docs WHERE created_by IS NULL;
--   2) NULL 인 문서가 있으면 'system:unknown' 으로 채움 (또는 백엔드
--      POST /refinery/admin/migrate-authors 로 일괄 처리 후 여기 실행)
--
-- UPDATE public.wiki_docs SET created_by = 'system:unknown' WHERE created_by IS NULL;
-- UPDATE public.tasks      SET created_by = 'system:unknown' WHERE created_by IS NULL;
-- UPDATE public.wbs_nodes  SET created_by = 'system:unknown' WHERE created_by IS NULL;
-- UPDATE public.issues     SET created_by = 'system:unknown' WHERE created_by IS NULL;

-- ============================================================
-- 1. refinery_sessions 신규 테이블
-- ============================================================
CREATE TABLE IF NOT EXISTS public.refinery_sessions (
    id                  text PRIMARY KEY,
    project_id          text REFERENCES public.projects(id) ON DELETE CASCADE,
    title               text NOT NULL,
    -- 작성자 정책 (★ 절대 NULL 허용 금지)
    created_by          text NOT NULL,
    updated_by          text,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    -- 세션 상태: draft|decomposing|classifying|approving|composing|ready|published|archived
    status              text DEFAULT 'draft',
    -- vault 원본 문서 ID 배열
    vault_doc_ids       jsonb DEFAULT '[]'::jsonb,
    -- 분해 결과 노드 트리 (each with history[], source_refs[])
    nodes               jsonb DEFAULT '[]'::jsonb,
    -- {node_id: 'canon'|'hyp'|'later'|'cut'}
    classifications     jsonb DEFAULT '{}'::jsonb,
    -- 분류 참여자 user_id[]
    participants        jsonb DEFAULT '[]'::jsonb,
    -- 결재 발행 시 review_requests.id
    review_request_id   text,
    -- 정의서 트리 미리보기 ({files: [{path, body, target_kind, ...}]})
    generated_tree      jsonb,
    -- 위키 적용 후 생성된 wiki_docs.id[]
    generated_doc_ids   jsonb DEFAULT '[]'::jsonb,
    -- 작업 생성 후 ID 배열
    generated_wbs_ids   jsonb DEFAULT '[]'::jsonb,
    generated_task_ids  jsonb DEFAULT '[]'::jsonb,
    generated_issue_ids jsonb DEFAULT '[]'::jsonb,
    -- 버전 관계: v2 면 v1 의 id (parent_session_id)
    parent_session_id   text REFERENCES public.refinery_sessions(id) ON DELETE SET NULL,
    version_label       text DEFAULT 'v1',
    -- 세션 전체 변경 이력
    history             jsonb DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_refinery_sessions_project   ON public.refinery_sessions(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_refinery_sessions_status    ON public.refinery_sessions(status);
CREATE INDEX IF NOT EXISTS idx_refinery_sessions_created_by ON public.refinery_sessions(created_by);
CREATE INDEX IF NOT EXISTS idx_refinery_sessions_parent    ON public.refinery_sessions(parent_session_id);

-- ============================================================
-- 2. updated_at 자동 갱신 트리거
-- ============================================================
CREATE OR REPLACE FUNCTION public._refinery_touch_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_refinery_sessions_touch ON public.refinery_sessions;
CREATE TRIGGER trg_refinery_sessions_touch
BEFORE UPDATE ON public.refinery_sessions
FOR EACH ROW EXECUTE FUNCTION public._refinery_touch_updated_at();

-- ============================================================
-- 3. wiki_docs.meta 확장 키 (DDL 변경 X — jsonb 안에 추가)
-- ============================================================
-- 다음 키들이 meta jsonb 안에 추가됨 (스키마 변경 불필요):
--   meta.vault                 = true         (vault 원본 — 편집 잠금)
--   meta.refinerySessionId    = 'rfs_xxx'    (역추적)
--   meta.refineryNodeIds      = [...]        (파생 노드 ID)
--   meta.archived_versions    = [...]        (덮어쓰기 보존, 최대 20)
--   meta.category             = canon|hyp|later|cut|overview|meta
--   meta.target_kind          = canon|wiki|personal
--   meta.playtest_status      = pending|positive|negative   (가설 파일)
--   meta.tags                 = [...]        (옵시디언 태그)
--   meta.user_edited_sections = [...]        (<!-- USER --> 마커 추적)
--   meta.owner                = user_id      (personal 일 때)
--   meta.visibility           = public|private (personal 일 때)

-- ============================================================
-- 4. 작성자 NOT NULL 강제 (★ 사전 NULL 백필 후 실행)
-- ============================================================
-- 4-1. wiki_docs.created_by NOT NULL
--   ALTER TABLE public.wiki_docs ALTER COLUMN created_by SET NOT NULL;
-- 4-2. tasks / wbs_nodes / issues — 필요 시 동일 패턴
--   ALTER TABLE public.tasks     ALTER COLUMN created_by SET NOT NULL;
--   ALTER TABLE public.wbs_nodes ALTER COLUMN created_by SET NOT NULL;
--   ALTER TABLE public.issues    ALTER COLUMN created_by SET NOT NULL;
-- ★ 주석 해제 전:
--   1) POST /refinery/admin/migrate-authors {table:'wiki_docs', dry_run:true} 로
--      NULL 카운트 확인
--   2) dry_run:false 로 채움 (fill_with='system:unknown' 또는 사용자 식별자)
--   3) 그 뒤 위 ALTER 주석 해제하고 실행

-- ============================================================
-- 5. RLS (Row Level Security) — Supabase 권장
-- ============================================================
ALTER TABLE public.refinery_sessions ENABLE ROW LEVEL SECURITY;

-- 5-1. SELECT — 프로젝트 참여자면 누구나 (또는 인증 후 모두)
DROP POLICY IF EXISTS refinery_sessions_select ON public.refinery_sessions;
CREATE POLICY refinery_sessions_select
    ON public.refinery_sessions
    FOR SELECT
    USING (true);  -- 모든 인증 유저 (project 단위 추가 제약은 앱 레벨에서)

-- 5-2. INSERT — created_by 가 auth.uid()::text 와 일치하거나, service_role 일 때
DROP POLICY IF EXISTS refinery_sessions_insert ON public.refinery_sessions;
CREATE POLICY refinery_sessions_insert
    ON public.refinery_sessions
    FOR INSERT
    WITH CHECK (
        created_by IS NOT NULL  -- ★ 누락 0 강제
        AND (created_by = auth.uid()::text OR auth.role() = 'service_role')
    );

-- 5-3. UPDATE — 본인 또는 참여자만
DROP POLICY IF EXISTS refinery_sessions_update ON public.refinery_sessions;
CREATE POLICY refinery_sessions_update
    ON public.refinery_sessions
    FOR UPDATE
    USING (
        updated_by IS NOT NULL
        AND (
            created_by = auth.uid()::text
            OR participants @> to_jsonb(auth.uid()::text)
            OR auth.role() = 'service_role'
        )
    );

-- 5-4. DELETE — 작성자 또는 service_role
DROP POLICY IF EXISTS refinery_sessions_delete ON public.refinery_sessions;
CREATE POLICY refinery_sessions_delete
    ON public.refinery_sessions
    FOR DELETE
    USING (created_by = auth.uid()::text OR auth.role() = 'service_role');

-- ============================================================
-- 6. wiki_docs RLS — created_by NOT NULL 가드 (선택)
-- ============================================================
-- ★ 이미 wiki_docs RLS 가 설정되어 있다면 정책 추가만:
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'wiki_docs_enforce_author') THEN
        CREATE POLICY wiki_docs_enforce_author
            ON public.wiki_docs
            FOR INSERT
            WITH CHECK (created_by IS NOT NULL);
    END IF;
EXCEPTION
    WHEN undefined_table THEN NULL;  -- wiki_docs 없으면 skip
END $$;

-- ============================================================
-- 7. 정리 — 인덱스 + 확인 쿼리
-- ============================================================
-- 7-1. 세션 통계
--   SELECT status, count(*) FROM public.refinery_sessions GROUP BY status;
-- 7-2. 작성자 누락 확인
--   SELECT 'wiki_docs' AS t, count(*) FROM public.wiki_docs WHERE created_by IS NULL
--   UNION ALL SELECT 'refinery_sessions', count(*) FROM public.refinery_sessions WHERE created_by IS NULL
--   UNION ALL SELECT 'tasks', count(*) FROM public.tasks WHERE created_by IS NULL
--   UNION ALL SELECT 'wbs_nodes', count(*) FROM public.wbs_nodes WHERE created_by IS NULL
--   UNION ALL SELECT 'issues', count(*) FROM public.issues WHERE created_by IS NULL;
-- 7-3. 세션별 vault 수
--   SELECT id, title, jsonb_array_length(vault_doc_ids) AS vault_n,
--          jsonb_array_length(nodes) AS node_n,
--          jsonb_array_length(generated_doc_ids) AS doc_n
--   FROM public.refinery_sessions ORDER BY updated_at DESC LIMIT 20;

-- ============================================================
-- 끝 — 적용 후 백엔드 GET /refinery/health 응답에 r223 노출 확인
-- ============================================================
