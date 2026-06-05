-- ============================================================
-- 연구 정련소 — 도출 Stream 컬럼 마이그레이션 (계획서 §6, 결정 #2)
-- 실행: Supabase SQL 편집기 또는 앱 [설정 > DB 스키마 점검]에서 수동 실행.
-- 모두 IF NOT EXISTS 라 여러 번 실행해도 안전(idempotent).
-- 컬럼이 없으면 백엔드 write 가드가 그 키만 제외하므로, 이 SQL 실행 전에도
-- 기존 기능은 깨지지 않음. 단 Stream 추적(계보 UI)은 컬럼 생성 후 동작.
-- ============================================================

-- ── tasks: 공정태그 + 출처노드 + 의존 + Stream ──────────────
alter table if exists tasks add column if not exists process_tag   text;          -- PM_TAX 소분류 id (예 '3.1.2')
alter table if exists tasks add column if not exists origin_node_id text;          -- 도출 출처 분해노드 id (키워드)
alter table if exists tasks add column if not exists depends_on     jsonb default '[]'::jsonb;  -- 선행 task id 배열
alter table if exists tasks add column if not exists stream_id      text;          -- 도출 묶음 id

-- ── sprints: STAGE 연결 + 순서 + Stream ─────────────────────
alter table if exists sprints add column if not exists stage_id  text;             -- 소속 STAGE id
alter table if exists sprints add column if not exists seq       int;              -- 도출 순서(1..N)
alter table if exists sprints add column if not exists stream_id text;

-- ── product_stages: Stream ──────────────────────────────────
alter table if exists product_stages add column if not exists stream_id text;

-- ── wbs_nodes: Stream (체인은 기존 links jsonb 로 표현) ──────
alter table if exists wbs_nodes add column if not exists stream_id text;

-- ── wiki_docs: Stream (정의·분류 축). 출처 키워드(origin_node_ids)는 기존 meta(jsonb)에 stash ──
alter table if exists wiki_docs add column if not exists stream_id text;

-- ── 조회 가속(선택) ─────────────────────────────────────────
create index if not exists idx_tasks_stream    on tasks(stream_id);
create index if not exists idx_sprints_stream  on sprints(stream_id);
create index if not exists idx_sprints_stage   on sprints(stage_id);
create index if not exists idx_stages_stream   on product_stages(stream_id);
create index if not exists idx_wbs_stream      on wbs_nodes(stream_id);
create index if not exists idx_wiki_stream     on wiki_docs(stream_id);

-- ============================================================
-- 위키 표준 분류체계 (계획서 §7-A, 결정 #5)
--  WIKI_TAX(프론트 상수)는 기본 제공. 프로젝트 커스텀만 아래 테이블에 저장.
--  product_taxonomy 와 동일 패턴.
-- ============================================================
create table if not exists wiki_taxonomy (
  id          text primary key,
  project_id  text,
  i           text not null,          -- 분류 id (예 '1.1')
  p           text,                   -- 부모 id (예 '1')
  v           int  not null default 1,-- 레벨(1 대분류 / 2 중분류 / 3 소분류)
  s           text not null,          -- 축약명
  l           text default '',        -- 전체명/설명
  sort_order  bigint,
  created_by  text,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);
create index if not exists idx_wiki_tax_project on wiki_taxonomy(project_id);

-- ============================================================
-- 확인 쿼리 — 아래 결과로 컬럼/테이블 생성 여부 점검
-- ============================================================
-- 신규 컬럼 확인:
select table_name, column_name
  from information_schema.columns
 where (table_name='tasks'          and column_name in ('process_tag','origin_node_id','depends_on','stream_id'))
    or (table_name='sprints'        and column_name in ('stage_id','seq','stream_id'))
    or (table_name='product_stages' and column_name in ('stream_id'))
    or (table_name='wbs_nodes'      and column_name in ('stream_id'))
 order by table_name, column_name;

-- wiki_taxonomy 테이블 확인:
select count(*) as wiki_taxonomy_exists
  from information_schema.tables
 where table_name='wiki_taxonomy';
