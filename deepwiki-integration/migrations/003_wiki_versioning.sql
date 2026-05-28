-- ════════════════════════════════════════════════════════════════════
-- Deep Wiki r121 — Repo별 구분 + 생성 버전 관리
-- 002_wiki_pages.sql 위에 컬럼만 추가 (멱등)
-- ════════════════════════════════════════════════════════════════════

-- 1. repo_name (git_url에서 추출, 예: 'pB', 'tda-dashboard')
alter table deep_wiki_pages
    add column if not exists repo_name text;
create index if not exists deep_wiki_pages_repo_idx
    on deep_wiki_pages(project_id, repo_name);

-- 2. generation_id (생성 회차, 같은 repo의 여러 버전 구분)
--    timestamp 기반 string (예: '2026-05-29T12-16-53')
alter table deep_wiki_pages
    add column if not exists generation_id text;
create index if not exists deep_wiki_pages_gen_idx
    on deep_wiki_pages(project_id, repo_name, generation_id);

-- 3. is_latest 플래그 — 빠른 최신 버전 조회용
alter table deep_wiki_pages
    add column if not exists is_latest boolean default true;

-- 4. 기존 페이지(repo_name/generation_id 없는 행)는 git_url에서 repo 추출 + 'legacy' 생성id 부여
update deep_wiki_pages
set repo_name = coalesce(
        repo_name,
        regexp_replace(coalesce(git_url, ''), '.*/([^/]+?)(\.git)?$', '\1')
    ),
    generation_id = coalesce(generation_id, 'legacy')
where repo_name is null or generation_id is null;

do $$ begin raise notice '✅ Deep Wiki r121: repo_name + generation_id + is_latest 컬럼 적용 완료'; end $$;
