-- ============================================================
-- TDA v40 — Supabase Realtime publication 활성화
-- ============================================================
-- 실행 위치: Supabase Dashboard → SQL Editor → New Query
-- 효과: postgres_changes(실시간 협업) 이벤트가 작동
-- ============================================================

-- 1. supabase_realtime publication에 모든 v40 테이블 추가
-- (이미 추가된 테이블은 자동으로 무시됨 - DO 블록 사용)
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'projects', 'kanban_categories', 'tasks', 'wiki_docs',
        'task_comments', 'tag_colors', 'users', 'sprints', 'review_requests',
        'entity_comments'  -- [r150] 이슈/일정/에셋 코멘트 + 대댓글 실시간
    ])
    LOOP
        BEGIN
            EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
        EXCEPTION WHEN duplicate_object THEN
            -- 이미 추가된 테이블 — 무시
            RAISE NOTICE 'Table % already in publication', t;
        END;
    END LOOP;
END $$;

-- 2. 검증: publication에 포함된 테이블 목록 조회
SELECT schemaname, tablename 
FROM pg_publication_tables 
WHERE pubname = 'supabase_realtime'
ORDER BY tablename;
-- 위 9개 테이블이 모두 보여야 함
