-- ============================================================
-- TDA Dashboard v40 — Supabase 초기 스키마 마이그레이션
-- ============================================================
-- 대상 프로젝트: xrmfunjekgwhoxtltzrj
-- 실행 위치: Supabase Dashboard → SQL Editor → New Query
-- 실행 방법: 전체 복사 → 붙여넣기 → "Run" 클릭
-- 멱등성: 모든 명령은 IF NOT EXISTS / OR REPLACE 사용 → 여러 번 실행해도 안전
-- ============================================================

-- ============================================================
-- 1. v39 호환 테이블 (기존 데이터 모델)
-- ============================================================

-- 1-1. projects: 워크스페이스(프로젝트)
CREATE TABLE IF NOT EXISTS public.projects (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    category    text DEFAULT '일반',
    created_at  timestamptz DEFAULT now()
);

-- 1-2. kanban_categories: 칸반 카테고리
CREATE TABLE IF NOT EXISTS public.kanban_categories (
    id          text PRIMARY KEY,
    project_id  text REFERENCES public.projects(id) ON DELETE CASCADE,
    title       text NOT NULL,
    subtitle    text DEFAULT '',
    sort_order  int DEFAULT 0,
    created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kanban_categories_project ON public.kanban_categories(project_id, sort_order);

-- 1-3. tasks: 태스크 (카드)
CREATE TABLE IF NOT EXISTS public.tasks (
    id              text PRIMARY KEY,
    project_id      text REFERENCES public.projects(id) ON DELETE CASCADE,
    cat_id          text REFERENCES public.kanban_categories(id) ON DELETE SET NULL,
    title           text NOT NULL DEFAULT '제목 없음',
    description     text DEFAULT '',
    details         text DEFAULT '',
    priority        text DEFAULT 'p2',   -- p0/p1/p2
    status          text DEFAULT 'pending',  -- pending/progress/completed
    dev_name        text DEFAULT 'Dev A',
    cat_badge       text DEFAULT '',
    is_starred      boolean DEFAULT false,
    completed_at    text,                -- 완료 시각 표시용 (text로 v39 호환)
    scripts         jsonb DEFAULT '[]'::jsonb,
    sort_order      int DEFAULT 0,
    -- [v40] 신규 필드
    zone            text DEFAULT 'shelf',  -- now/shelf/buried
    sprint_id       text,
    bury_reason     text,
    bury_history    jsonb DEFAULT '[]'::jsonb,
    carryover_count int DEFAULT 0,
    done_at         timestamptz,
    linked_doc_ids  jsonb DEFAULT '[]'::jsonb,
    created_by      uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    assignees       jsonb DEFAULT '[]'::jsonb,
    updated_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON public.tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_cat ON public.tasks(cat_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_tasks_zone ON public.tasks(zone);
CREATE INDEX IF NOT EXISTS idx_tasks_sprint ON public.tasks(sprint_id);

-- 1-4. wiki_docs: 위키 문서
CREATE TABLE IF NOT EXISTS public.wiki_docs (
    id              text PRIMARY KEY,
    project_id      text REFERENCES public.projects(id) ON DELETE CASCADE,
    parent_id       text,
    title           text NOT NULL DEFAULT '제목 없음',
    content         text DEFAULT '',
    emoji           text DEFAULT '📄',
    sort_order      int DEFAULT 0,
    is_collapsed    boolean DEFAULT false,
    is_deprecated   boolean DEFAULT false,
    -- [v40] 신규 필드
    doc_type        text DEFAULT 'plan', -- plan/tech/decision/reference/ops
    linked_task_ids jsonb DEFAULT '[]'::jsonb,
    linked_doc_ids  jsonb DEFAULT '[]'::jsonb,
    is_locked       boolean DEFAULT false,
    ref_meta        jsonb DEFAULT '{}'::jsonb,
    created_by      uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    updated_by      uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    update_history  jsonb DEFAULT '[]'::jsonb,
    updated_at      bigint DEFAULT extract(epoch from now())*1000  -- v39는 ms 타임스탬프
);
CREATE INDEX IF NOT EXISTS idx_wiki_docs_project ON public.wiki_docs(project_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_wiki_docs_parent ON public.wiki_docs(parent_id);
CREATE INDEX IF NOT EXISTS idx_wiki_docs_type ON public.wiki_docs(doc_type);

-- 1-5. task_comments: 코멘트
CREATE TABLE IF NOT EXISTS public.task_comments (
    id           bigserial PRIMARY KEY,
    task_id      text REFERENCES public.tasks(id) ON DELETE CASCADE,
    author       text DEFAULT '익명',
    author_id    uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    content      text NOT NULL,
    created_time text,                    -- "14:30" 형식 (v39 호환)
    ts           bigint NOT NULL          -- ms 타임스탬프 (정렬용)
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON public.task_comments(task_id, ts);

-- 1-6. tag_colors: 태그 색상
CREATE TABLE IF NOT EXISTS public.tag_colors (
    tag_name  text PRIMARY KEY,
    color     text NOT NULL DEFAULT '#495057'
);

-- ============================================================
-- 2. v40 신규 테이블
-- ============================================================

-- 2-1. users: 팀원 (auth.users와 연동된 공개 테이블)
CREATE TABLE IF NOT EXISTS public.users (
    id            uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email         text UNIQUE,
    display_name  text,
    avatar_url    text,
    provider      text,   -- 'google' | 'github'
    role          text DEFAULT 'member',   -- 'admin' | 'member'
    joined_at     timestamptz DEFAULT now()
);

-- 2-2. sprints: 스프린트
CREATE TABLE IF NOT EXISTS public.sprints (
    id                  text PRIMARY KEY,    -- 예: 'sprint-2026W22'
    project_id          text REFERENCES public.projects(id) ON DELETE CASCADE,
    week_label          text NOT NULL,
    goal                text DEFAULT '',
    milestone_criteria  jsonb DEFAULT '[]'::jsonb,
    status              text DEFAULT 'planning',  -- planning/active/review/closed
    intrusion_count     int DEFAULT 0,
    intrusion_log       jsonb DEFAULT '[]'::jsonb,
    carryover_from_previous jsonb DEFAULT '[]'::jsonb,
    start_date          date,
    end_date            date,
    created_at          timestamptz DEFAULT now(),
    closed_at           timestamptz
);
CREATE INDEX IF NOT EXISTS idx_sprints_project ON public.sprints(project_id, status);
-- [v40 Day2 청크 4] 스프린트 시작/종료 audit trail (누가·언제 시작/종료했는지)
--   기존 배포(테이블 이미 생성됨)에도 안전하게 적용되는 idempotent 패치.
ALTER TABLE public.sprints ADD COLUMN IF NOT EXISTS history jsonb DEFAULT '[]'::jsonb;

-- 2-3. review_requests: 리뷰 요청 (의사결정 일반 메커니즘)
CREATE TABLE IF NOT EXISTS public.review_requests (
    id              text PRIMARY KEY,
    type            text NOT NULL,           -- 'intrusion' | 'standard_change' | 'decision'
    rule            jsonb NOT NULL,          -- { type, requiredApprovers[], minApprovals }
    target_task_id  text REFERENCES public.tasks(id) ON DELETE SET NULL,
    target_doc_id   text REFERENCES public.wiki_docs(id) ON DELETE SET NULL,
    sprint_id       text REFERENCES public.sprints(id) ON DELETE SET NULL,
    project_id      text REFERENCES public.projects(id) ON DELETE CASCADE,
    proposer_id     uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    proposer_name   text,
    reason          text NOT NULL,
    votes           jsonb DEFAULT '[]'::jsonb,
    status          text DEFAULT 'pending',  -- pending/approved/rejected/expired/forced
    decision_tag    jsonb,
    created_at      timestamptz DEFAULT now(),
    expires_at      timestamptz,
    decided_at      timestamptz
);
CREATE INDEX IF NOT EXISTS idx_review_requests_status ON public.review_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_review_requests_target ON public.review_requests(target_task_id, target_doc_id);

-- ============================================================
-- 3. 트리거: auth.users 생성 시 public.users 자동 동기화
-- ============================================================
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
    INSERT INTO public.users (id, email, display_name, avatar_url, provider)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(
            NEW.raw_user_meta_data->>'full_name',
            NEW.raw_user_meta_data->>'name',
            NEW.raw_user_meta_data->>'user_name',
            split_part(NEW.email, '@', 1)
        ),
        COALESCE(
            NEW.raw_user_meta_data->>'avatar_url',
            NEW.raw_user_meta_data->>'picture'
        ),
        COALESCE(NEW.raw_app_meta_data->>'provider', 'unknown')
    )
    ON CONFLICT (id) DO UPDATE SET
        email        = EXCLUDED.email,
        display_name = EXCLUDED.display_name,
        avatar_url   = EXCLUDED.avatar_url,
        provider     = EXCLUDED.provider;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT OR UPDATE ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- 기존에 가입한 사용자도 public.users에 백필
INSERT INTO public.users (id, email, display_name, avatar_url, provider)
SELECT 
    id, email,
    COALESCE(raw_user_meta_data->>'full_name', raw_user_meta_data->>'name', raw_user_meta_data->>'user_name', split_part(email, '@', 1)),
    COALESCE(raw_user_meta_data->>'avatar_url', raw_user_meta_data->>'picture'),
    COALESCE(raw_app_meta_data->>'provider', 'unknown')
FROM auth.users
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 4. RLS (Row Level Security) 활성화 + 기본 정책
-- ============================================================
-- 정책: "로그인한 사용자는 모두 읽기/쓰기 가능"
-- 추후 특정 이메일만 허용하는 strict mode로 조이려면 §4-B 사용

ALTER TABLE public.projects          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kanban_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tasks             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wiki_docs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_comments     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tag_colors        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sprints           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.review_requests   ENABLE ROW LEVEL SECURITY;

-- 정책 작성: 인증된 사용자만 모든 작업 허용 (LOOSE MODE)
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'projects', 'kanban_categories', 'tasks', 'wiki_docs',
        'task_comments', 'tag_colors', 'users', 'sprints', 'review_requests'
    ])
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS "Authenticated all" ON public.%I', t);
        EXECUTE format(
            'CREATE POLICY "Authenticated all" ON public.%I FOR ALL TO authenticated USING (true) WITH CHECK (true)',
            t
        );
    END LOOP;
END $$;

-- ============================================================
-- 5. 기본 프로젝트 생성 (v39 코드는 activeProjectId='default'로 시작)
-- ============================================================
INSERT INTO public.projects (id, name, category)
VALUES ('default', 'pB-3 COMBAT', '메인 프로젝트')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 완료!
-- ============================================================
-- 확인:
--   SELECT table_name FROM information_schema.tables 
--   WHERE table_schema='public' ORDER BY table_name;
-- 
-- 9개 테이블이 보이면 성공:
--   kanban_categories, projects, review_requests, sprints,
--   tag_colors, task_comments, tasks, users, wiki_docs
-- ============================================================


-- ============================================================
-- (선택) §4-B: STRICT MODE — 특정 이메일만 허용
-- ============================================================
-- 위 LOOSE MODE는 "Supabase auth로 로그인한 누구나" 접근 가능.
-- 팀원 외부 접근을 차단하려면 아래를 실행하여 정책 교체:
--
-- DO $$
-- DECLARE
--     t text;
--     allowed_emails text[] := ARRAY[
--         'rnk505@gmail.com',
--         'teammate2@gmail.com',
--         'teammate3@gmail.com'
--     ];
-- BEGIN
--     FOR t IN SELECT unnest(ARRAY[
--         'projects', 'kanban_categories', 'tasks', 'wiki_docs',
--         'task_comments', 'tag_colors', 'users', 'sprints', 'review_requests'
--     ])
--     LOOP
--         EXECUTE format('DROP POLICY IF EXISTS "Authenticated all" ON public.%I', t);
--         EXECUTE format('DROP POLICY IF EXISTS "Team only" ON public.%I', t);
--         EXECUTE format(
--             'CREATE POLICY "Team only" ON public.%I FOR ALL TO authenticated USING ((SELECT email FROM auth.users WHERE id=auth.uid()) = ANY($1)) WITH CHECK ((SELECT email FROM auth.users WHERE id=auth.uid()) = ANY($1))',
--             t
--         ) USING allowed_emails;
--     END LOOP;
-- END $$;
