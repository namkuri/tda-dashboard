-- ════════════════════════════════════════════════════════════════════
-- Deep Wiki — pgvector 청크 저장소
-- Supabase SQL Editor에서 1회 실행 (멱등)
-- ════════════════════════════════════════════════════════════════════

-- 1. pgvector 확장 활성화
create extension if not exists vector;

-- 2. 청크 테이블
create table if not exists doc_chunks (
    id              bigserial primary key,
    project_id      text,                          -- 프로젝트별 격리 (null = global)
    source_type     text not null,                 -- 'code' | 'wiki' | 'task' | 'sprint' | 'asset' | ...
    source_id       text not null,                 -- 원본 id (파일경로·doc_id·task_id 등)
    source_path     text,                          -- 파일 경로 또는 trail (UI 표시용)
    source_title    text,                          -- 사람이 읽을 제목
    chunk_idx       int not null default 0,        -- 같은 source 내 청크 순번
    content         text not null,
    embedding       vector(768),                   -- nomic-embed-text 차원
    token_count     int,
    created_at      timestamptz default now(),
    updated_at      timestamptz default now()
);

-- 3. 인덱스
create unique index if not exists doc_chunks_uniq
    on doc_chunks(source_type, source_id, chunk_idx);
create index if not exists doc_chunks_project_idx
    on doc_chunks(project_id);
create index if not exists doc_chunks_source_type_idx
    on doc_chunks(source_type);

-- 4. 벡터 유사도 검색 인덱스 (IVFFlat — 작은 데이터셋엔 충분)
-- 더 큰 데이터셋(>100만 행)에는 HNSW 권장: using hnsw (embedding vector_cosine_ops)
create index if not exists doc_chunks_embedding_idx
    on doc_chunks
    using ivfflat (embedding vector_cosine_ops)
    with (lists = 100);

-- 5. RLS 비활성화 (기존 테이블과 동일 정책 — service_role로 접근)
alter table doc_chunks disable row level security;

-- 6. 검색 함수 (RPC) — top-k 코사인 유사도
create or replace function match_doc_chunks(
    query_embedding vector(768),
    match_project_id text default null,
    source_types text[] default null,
    match_count int default 8
)
returns table (
    id bigint,
    project_id text,
    source_type text,
    source_id text,
    source_path text,
    source_title text,
    content text,
    similarity float
)
language plpgsql
as $$
begin
    return query
    select
        doc_chunks.id,
        doc_chunks.project_id,
        doc_chunks.source_type,
        doc_chunks.source_id,
        doc_chunks.source_path,
        doc_chunks.source_title,
        doc_chunks.content,
        1 - (doc_chunks.embedding <=> query_embedding) as similarity
    from doc_chunks
    where doc_chunks.embedding is not null
      and (match_project_id is null or doc_chunks.project_id = match_project_id or doc_chunks.project_id is null)
      and (source_types is null or doc_chunks.source_type = any(source_types))
    order by doc_chunks.embedding <=> query_embedding
    limit match_count;
end;
$$;

-- 7. 통계 뷰 (관리용)
create or replace view doc_chunks_stats as
select
    source_type,
    coalesce(project_id, 'global') as project_id,
    count(*) as chunk_count,
    sum(token_count) as total_tokens,
    max(updated_at) as last_updated
from doc_chunks
group by source_type, project_id
order by source_type, project_id;

-- 완료 메시지
do $$ begin raise notice '✅ doc_chunks 테이블 + match_doc_chunks RPC + 인덱스 생성 완료'; end $$;
