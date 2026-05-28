-- ════════════════════════════════════════════════════════════════════
-- Deep Wiki — 자동 생성 위키 페이지 + 기획 대조 보고서
-- r112 골격, r113 자동 생성기에서 채워짐
-- Supabase SQL Editor에서 1회 실행 (멱등)
-- ════════════════════════════════════════════════════════════════════

-- 1. 자동 생성 위키 페이지 (1차 MD)
--    유니티 Git 레포를 LLM이 분석해 만든 페이지들.
--    예: "시스템 개요", "폴더 구조", "주요 매니저", "데이터 모델", "의존성 그래프" 등.
create table if not exists deep_wiki_pages (
    id              text primary key,
    project_id      text not null,
    git_url         text,                                  -- 어떤 레포에서 생성됐나
    git_commit      text,                                  -- 생성 시점 커밋 해시
    slug            text not null,                         -- url-safe id (예: "system-overview", "kanban-system")
    title           text not null,                         -- 사람이 읽을 제목
    parent_slug     text,                                  -- 트리 계층용
    sort_order      int default 0,
    summary         text,                                  -- 1~2줄 요약 (트리 hover)
    content         text not null,                         -- 마크다운 본문
    meta            jsonb default '{}'::jsonb,             -- {tags, related_files, complexity, ...}
    created_at      timestamptz default now(),
    updated_at      timestamptz default now()
);
create index if not exists deep_wiki_pages_proj_idx on deep_wiki_pages(project_id);
create index if not exists deep_wiki_pages_slug_idx on deep_wiki_pages(project_id, slug);
alter table deep_wiki_pages disable row level security;

-- 2. 기획 대조 보고서 (2차 MD)
--    1차 위키 페이지 + 프로젝트 위키(canon kind 문서) 대조 → 일치도/누락/차이 분석.
create table if not exists deep_wiki_audits (
    id              text primary key,
    project_id      text not null,
    title           text not null,                         -- "전투 시스템 일치도 보고서" 등
    summary         text,                                  -- 요약
    content         text not null,                         -- 마크다운 본문 (표·차이점·결론)
    related_pages   jsonb default '[]'::jsonb,             -- ["page_slug_1", "page_slug_2"]
    related_canons  jsonb default '[]'::jsonb,             -- canon doc_id 배열
    match_score     real,                                  -- 0.0~1.0 일치도
    findings        jsonb default '[]'::jsonb,             -- [{type:'missing'|'mismatch'|'extra', text, severity}]
    created_at      timestamptz default now(),
    updated_at      timestamptz default now()
);
create index if not exists deep_wiki_audits_proj_idx on deep_wiki_audits(project_id);
alter table deep_wiki_audits disable row level security;

-- 완료 메시지
do $$ begin raise notice '✅ Deep Wiki 페이지·감사 테이블 생성 완료 (r113~에서 데이터 채워짐)'; end $$;
